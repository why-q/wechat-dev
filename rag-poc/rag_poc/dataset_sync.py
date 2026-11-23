"""One-shot, resumable collector for WeChat customer-service history."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
HISTORY_URL = "https://api.weixin.qq.com/customservice/msgrecord/getmsglist"
PAGE_SIZE = 10_000
CHINA_TZ = ZoneInfo("Asia/Shanghai")
CSV_HEADER = ("record_key", "message_time_utc", "question", "source")
_STATE_FIELDS = frozenset(
    {"range_start_date", "range_end_date", "next_date", "next_msgid", "updated_at_utc"}
)


class DatasetSyncError(Exception):
    """Safe operator-facing error with a stable code and no upstream payload."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        cause: BaseException | None = None,
        api_errcode: int | None = None,
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.api_errcode = api_errcode
        super().__init__(f"[{code}] {public_message}")


@dataclass(frozen=True)
class HistoryPage:
    records: list[dict[str, object]]
    number: int
    next_msgid: int


@dataclass(frozen=True)
class HistoryQuestion:
    record_key: str
    text: str
    occurred_at_utc: datetime


@dataclass(frozen=True)
class SyncCursor:
    range_start_date: date
    range_end_date: date
    next_date: date
    next_msgid: int


@dataclass(frozen=True)
class SyncStats:
    pages_fetched: int
    inbound_records: int
    inserted_rows: int


def _is_int(value: object) -> bool:
    return type(value) is int


def _date_value(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _raise_storage_error(code: str, message: str, exc: BaseException) -> DatasetSyncError:
    return DatasetSyncError(code, message, cause=exc)


class WeChatHistoryClient:
    """Direct HTTPS client for the official-account getmsglist API."""

    def __init__(self, app_id: str, app_secret: str, *, timeout_seconds: float = 5.0) -> None:
        if not isinstance(app_id, str) or not app_id.strip():
            raise DatasetSyncError("dataset_credentials_missing", "WECHAT_APP_ID is required.")
        if not isinstance(app_secret, str) or not app_secret.strip():
            raise DatasetSyncError("dataset_credentials_missing", "WECHAT_APP_SECRET is required.")
        try:
            app_id.encode("utf-8")
            app_secret.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DatasetSyncError(
                "dataset_credentials_invalid", "The WeChat credentials are invalid.", cause=exc
            ) from None
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise DatasetSyncError("dataset_config_invalid", "The HTTP timeout must be positive.")
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout_seconds = float(timeout_seconds)
        self._access_token: str | None = None
        self._token_refresh_at = 0.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WeChatHistoryClient:
        return cls(env.get("WECHAT_APP_ID"), env.get("WECHAT_APP_SECRET"))

    def _request_json(self, request: Request) -> dict[str, object]:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read()
        except Exception as exc:
            raise DatasetSyncError(
                "wechat_network_error", "The WeChat API request failed.", cause=exc
            ) from None
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise DatasetSyncError(
                "wechat_response_invalid", "The WeChat API returned invalid JSON.", cause=exc
            ) from None
        if not isinstance(decoded, dict):
            raise DatasetSyncError(
                "wechat_response_invalid", "The WeChat API returned an invalid response shape."
            )
        return decoded

    @staticmethod
    def _check_errcode(response: Mapping[str, object]) -> None:
        errcode = response.get("errcode", 0)
        if not _is_int(errcode):
            raise DatasetSyncError(
                "wechat_response_invalid", "The WeChat API returned an invalid response shape."
            )
        if errcode != 0:
            raise DatasetSyncError(
                "wechat_api_error",
                "The WeChat API rejected the request.",
                api_errcode=errcode,
            )

    def _request_token(self) -> str:
        query = urlencode(
            {
                "grant_type": "client_credential",
                "appid": self._app_id,
                "secret": self._app_secret,
            }
        )
        response = self._request_json(Request(f"{TOKEN_URL}?{query}", method="GET"))
        self._check_errcode(response)
        token = response.get("access_token")
        expires_in = response.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or not _is_int(expires_in)
            or expires_in <= 0
        ):
            raise DatasetSyncError(
                "wechat_response_invalid", "The WeChat API returned an invalid token response."
            )
        try:
            token.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DatasetSyncError(
                "wechat_response_invalid", "The WeChat API returned an invalid token response.", cause=exc
            ) from None
        self._access_token = token
        self._token_refresh_at = time.monotonic() + max(0, expires_in - 60)
        return token

    def _get_access_token(self) -> str:
        if self._access_token is None or time.monotonic() >= self._token_refresh_at:
            return self._request_token()
        return self._access_token

    def _fetch_page_with_token(
        self,
        token: str,
        *,
        start_time: int,
        end_time: int,
        msgid: int,
        number: int,
    ) -> HistoryPage:
        requested_number = number
        url = f"{HISTORY_URL}?{urlencode({'access_token': token})}"
        body = json.dumps(
            {"starttime": start_time, "endtime": end_time, "msgid": msgid, "number": number},
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = self._request_json(request)
        self._check_errcode(response)
        records = response.get("recordlist")
        response_number = response.get("number")
        if (
            not isinstance(records, list)
            or any(not isinstance(record, dict) for record in records)
            or not _is_int(response_number)
            or response_number < 0
            or len(records) > requested_number
        ):
            raise DatasetSyncError(
                "wechat_response_invalid", "The WeChat API returned an invalid history page."
            )

        if len(records) == requested_number:
            response_msgid = response.get("msgid")
            if not _is_int(response_msgid) or response_msgid <= 0 or response_msgid == msgid:
                raise DatasetSyncError(
                    "wechat_pagination_invalid",
                    "A full history page did not provide a changed positive cursor.",
                )
            next_msgid = response_msgid
        else:
            # The API's short-page msgid is optional and never drives pagination.
            next_msgid = msgid
        return HistoryPage(records=records, number=response_number, next_msgid=next_msgid)

    def fetch_page(
        self,
        *,
        start_time: int,
        end_time: int,
        msgid: int,
        number: int = PAGE_SIZE,
    ) -> HistoryPage:
        if (
            not _is_int(start_time)
            or not _is_int(end_time)
            or start_time < 0
            or end_time < start_time
            or not _is_int(msgid)
            or msgid <= 0
            or not _is_int(number)
            or not 0 < number <= PAGE_SIZE
        ):
            raise DatasetSyncError("dataset_request_invalid", "The history page request is invalid.")

        for attempt in range(2):
            token = self._get_access_token()
            try:
                return self._fetch_page_with_token(
                    token,
                    start_time=start_time,
                    end_time=end_time,
                    msgid=msgid,
                    number=number,
                )
            except DatasetSyncError as exc:
                if exc.api_errcode != 40001 or attempt != 0:
                    raise
                self._access_token = None
                self._token_refresh_at = 0.0
        raise DatasetSyncError("wechat_api_error", "The WeChat API rejected the request.")


class _PhysicalCsvLines:
    """Decode physical CSV lines while exposing the byte offset of the next row."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self.next_offset = stream.tell()
        self._first_line = True

    def __iter__(self) -> _PhysicalCsvLines:
        return self

    def __next__(self) -> str:
        raw_line = self._stream.readline()
        if not raw_line:
            raise StopIteration
        self.next_offset = self._stream.tell()
        encoding = "utf-8-sig" if self._first_line else "utf-8"
        self._first_line = False
        return raw_line.decode(encoding)


class CsvDatasetStore:
    """Private CSV and atomic cursor storage; callers hold ``locked()`` while syncing."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.csv_path = self.output_dir / "questions.csv"
        self.state_path = self.output_dir / "sync-state.json"
        self.lock_path = self.output_dir / ".sync.lock"
        self._record_keys: set[str] | None = None

    def _ensure_directory(self) -> None:
        created = False
        try:
            try:
                status = self.output_dir.lstat()
            except FileNotFoundError:
                try:
                    self.output_dir.mkdir(mode=0o700, parents=True)
                    created = True
                except FileExistsError:
                    pass
                status = self.output_dir.lstat()
            if not stat.S_ISDIR(status.st_mode):
                raise DatasetSyncError(
                    "dataset_directory_invalid", "The dataset output path must be a real directory."
                )
            if created:
                descriptor = os.open(
                    self.output_dir,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fchmod(descriptor, 0o700)
                finally:
                    os.close(descriptor)
            elif stat.S_IMODE(status.st_mode) & 0o077 or stat.S_IMODE(status.st_mode) & 0o700 != 0o700:
                raise DatasetSyncError(
                    "dataset_directory_not_private",
                    "The existing dataset output directory must be private (mode 0700).",
                )
        except DatasetSyncError:
            raise
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_storage_error", "The dataset output directory is unavailable.", exc
            ) from None

    def _fsync_directory(self) -> None:
        descriptor = os.open(
            self.output_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DatasetSyncError(
                    "dataset_file_invalid", "A dataset file path is not a regular file."
                )
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _ensure_lock_file(self) -> None:
        try:
            descriptor = self._open_regular(self.lock_path, os.O_CREAT | os.O_RDWR)
            os.close(descriptor)
        except DatasetSyncError:
            raise
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_storage_error", "The dataset lock file is unavailable.", exc
            ) from None

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._ensure_directory()
        descriptor: int | None = None
        try:
            descriptor = self._open_regular(self.lock_path, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DatasetSyncError(
                    "dataset_lock_unavailable", "Another dataset sync is already running.", cause=exc
                ) from None
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except DatasetSyncError:
            raise
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_storage_error", "The dataset lock could not be acquired.", exc
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def initialize(self) -> None:
        self._ensure_directory()
        self._ensure_lock_file()
        try:
            descriptor = os.open(
                self.csv_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            try:
                descriptor = self._open_regular(self.csv_path, os.O_RDWR)
                os.close(descriptor)
            except DatasetSyncError:
                raise
            except OSError as exc:
                raise _raise_storage_error(
                    "dataset_storage_error", "The questions CSV is unavailable.", exc
                ) from None
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_storage_error", "The questions CSV could not be created.", exc
            ) from None
        else:
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
                    csv.writer(stream, lineterminator="\r\n").writerow(CSV_HEADER)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._fsync_directory()
            except OSError as exc:
                raise _raise_storage_error(
                    "dataset_storage_error", "The questions CSV could not be initialized.", exc
                ) from None
        self._record_keys = None

    def _truncate_csv(self, stream, offset: int) -> None:
        try:
            stream.seek(offset)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_storage_error", "The incomplete final CSV record could not be repaired.", exc
            ) from None

    @staticmethod
    def _row_key(row: list[str]) -> str:
        if (
            len(row) != len(CSV_HEADER)
            or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None
            or row[3] != "getmsglist"
            or not row[1].endswith("Z")
        ):
            raise DatasetSyncError(
                "dataset_csv_invalid", "The questions CSV contains a malformed row."
            )
        try:
            occurred = datetime.fromisoformat(row[1][:-1] + "+00:00")
        except ValueError as exc:
            raise DatasetSyncError(
                "dataset_csv_invalid", "The questions CSV contains a malformed row.", cause=exc
            ) from None
        if occurred.utcoffset() != timedelta(0):
            raise DatasetSyncError(
                "dataset_csv_invalid", "The questions CSV contains a malformed row."
            )
        return row[0]

    def _scan_csv(self) -> set[str]:
        try:
            descriptor = self._open_regular(self.csv_path, os.O_RDWR)
            with os.fdopen(descriptor, "r+b") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size == 0:
                    raise DatasetSyncError(
                        "dataset_csv_invalid", "The questions CSV has an invalid header."
                    )
                stream.seek(size - 1)
                # Writer uses CRLF: a lone final CR is still an incomplete physical line.
                has_final_lf = stream.read(1) == b"\n"
                stream.seek(0)
                lines = _PhysicalCsvLines(stream)
                reader = csv.reader(lines, strict=True)
                try:
                    header = next(reader)
                except (StopIteration, csv.Error, UnicodeDecodeError) as exc:
                    raise DatasetSyncError(
                        "dataset_csv_invalid", "The questions CSV has an invalid header.", cause=exc
                    ) from None
                if tuple(header) != CSV_HEADER or (lines.next_offset == size and not has_final_lf):
                    raise DatasetSyncError(
                        "dataset_csv_invalid", "The questions CSV has an invalid header."
                    )

                keys: set[str] = set()
                while lines.next_offset < size:
                    # Roll back to this logical-record boundary, including multiline rows.
                    row_start = lines.next_offset
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    except (csv.Error, UnicodeDecodeError) as exc:
                        if not has_final_lf and lines.next_offset == size:
                            self._truncate_csv(stream, row_start)
                            return keys
                        raise DatasetSyncError(
                            "dataset_csv_invalid",
                            "The questions CSV contains malformed CSV data.",
                            cause=exc,
                        ) from None
                    if not has_final_lf and lines.next_offset == size:
                        self._truncate_csv(stream, row_start)
                        return keys
                    keys.add(self._row_key(row))
                return keys
        except DatasetSyncError:
            raise
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_storage_error", "The questions CSV could not be read.", exc
            ) from None

    def _load_existing_keys(self) -> set[str]:
        if self._record_keys is None:
            self._record_keys = self._scan_csv()
        return self._record_keys

    def load_cursor(
        self, *, range_start_date: date, range_end_date: date
    ) -> SyncCursor | None:
        if (
            not _date_value(range_start_date)
            or not _date_value(range_end_date)
            or range_start_date > range_end_date
        ):
            raise DatasetSyncError("dataset_range_invalid", "The requested date range is invalid.")
        self._load_existing_keys()
        try:
            descriptor = self._open_regular(self.state_path, os.O_RDONLY)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                state = json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _raise_storage_error(
                "dataset_state_invalid", "The saved sync cursor is unreadable.", exc
            ) from None
        if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
            raise DatasetSyncError("dataset_state_invalid", "The saved sync cursor is invalid.")
        try:
            start = date.fromisoformat(state["range_start_date"])
            end = date.fromisoformat(state["range_end_date"])
            next_day = date.fromisoformat(state["next_date"])
            updated = datetime.fromisoformat(state["updated_at_utc"].replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise DatasetSyncError(
                "dataset_state_invalid", "The saved sync cursor is invalid.", cause=exc
            ) from None
        next_msgid = state["next_msgid"]
        if (
            state["range_start_date"] != start.isoformat()
            or state["range_end_date"] != end.isoformat()
            or state["next_date"] != next_day.isoformat()
            or not _is_int(next_msgid)
            or next_msgid <= 0
            or start > end
            or next_day < start
            or next_day > end + timedelta(days=1)
            or updated.tzinfo is None
            or updated.utcoffset() != timedelta(0)
        ):
            raise DatasetSyncError("dataset_state_invalid", "The saved sync cursor is invalid.")
        if start != range_start_date or end != range_end_date:
            raise DatasetSyncError(
                "dataset_range_mismatch",
                "The saved cursor covers a different date range; use --reset to restart it.",
            )
        return SyncCursor(start, end, next_day, next_msgid)

    @staticmethod
    def _validate_question(question: HistoryQuestion) -> None:
        if (
            not isinstance(question, HistoryQuestion)
            or not isinstance(question.record_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", question.record_key) is None
            or not isinstance(question.text, str)
            or not isinstance(question.occurred_at_utc, datetime)
            or question.occurred_at_utc.tzinfo is None
        ):
            raise DatasetSyncError("dataset_record_invalid", "An inbound history record is invalid.")
        try:
            question.record_key.encode("utf-8")
            question.text.encode("utf-8")
            if question.occurred_at_utc.utcoffset() is None:
                raise ValueError("naive timestamp")
        except (UnicodeEncodeError, ValueError, OverflowError) as exc:
            raise DatasetSyncError(
                "dataset_record_invalid", "An inbound history record is invalid.", cause=exc
            ) from None

    @staticmethod
    def _validate_cursor(cursor: SyncCursor) -> None:
        if (
            not isinstance(cursor, SyncCursor)
            or not _date_value(cursor.range_start_date)
            or not _date_value(cursor.range_end_date)
            or not _date_value(cursor.next_date)
            or cursor.range_start_date > cursor.range_end_date
            or cursor.next_date < cursor.range_start_date
            or cursor.next_date > cursor.range_end_date + timedelta(days=1)
            or not _is_int(cursor.next_msgid)
            or cursor.next_msgid <= 0
        ):
            raise DatasetSyncError("dataset_state_invalid", "The next sync cursor is invalid.")

    def _write_cursor(self, cursor: SyncCursor) -> None:
        state = {
            "range_start_date": cursor.range_start_date.isoformat(),
            "range_end_date": cursor.range_end_date.isoformat(),
            "next_date": cursor.next_date.isoformat(),
            "next_msgid": cursor.next_msgid,
            "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        descriptor = -1
        temporary_path: str | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".sync-state.", suffix=".tmp", dir=self.output_dir
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                descriptor = -1
                json.dump(state, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
            self._fsync_directory()
        except OSError as exc:
            raise _raise_storage_error(
                "dataset_state_write_failed", "The sync cursor could not be saved.", exc
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def append_page(
        self,
        *,
        questions: Sequence[HistoryQuestion],
        next_cursor: SyncCursor,
    ) -> int:
        self._validate_cursor(next_cursor)
        if not self.csv_path.exists():
            self.initialize()
        keys = self._load_existing_keys()
        new_questions: list[HistoryQuestion] = []
        page_keys: set[str] = set()
        for question in questions:
            self._validate_question(question)
            if question.record_key not in keys and question.record_key not in page_keys:
                page_keys.add(question.record_key)
                new_questions.append(question)

        descriptor = -1
        try:
            descriptor = self._open_regular(self.csv_path, os.O_WRONLY | os.O_APPEND)
            with os.fdopen(descriptor, "a", encoding="utf-8", newline="") as stream:
                descriptor = -1
                writer = csv.writer(stream, lineterminator="\r\n")
                for question in new_questions:
                    occurred = question.occurred_at_utc.astimezone(timezone.utc)
                    message_time = occurred.isoformat().replace("+00:00", "Z")
                    writer.writerow((question.record_key, message_time, question.text, "getmsglist"))
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            self._record_keys = None
            raise _raise_storage_error(
                "dataset_storage_error", "The questions CSV could not be written.", exc
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        keys.update(page_keys)
        self._record_keys = keys
        self._write_cursor(next_cursor)
        return len(new_questions)


def make_record_key(record: Mapping[str, object], hash_key: str) -> str:
    """Return an HMAC digest over the selected fields without retaining OpenID."""
    if not isinstance(hash_key, str) or not hash_key:
        raise DatasetSyncError("dataset_hash_key_missing", "WECHAT_DATASET_HASH_KEY is required.")
    try:
        key_bytes = hash_key.encode("utf-8")
        canonical = {
            "openid": record["openid"],
            "opercode": record["opercode"],
            "text": record["text"],
            "time": record["time"],
            "worker": record.get("worker", ""),
        }
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DatasetSyncError(
            "dataset_record_invalid", "An inbound history record is invalid.", cause=exc
        ) from None
    return hmac.new(key_bytes, encoded, hashlib.sha256).hexdigest()


def _now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def _question_from_record(record: Mapping[str, object], hash_key: str) -> HistoryQuestion | None:
    opercode = record.get("opercode")
    if not _is_int(opercode):
        raise DatasetSyncError("dataset_record_invalid", "A history record has an invalid message type.")
    if opercode == 2002:
        return None
    if opercode != 2003:
        return None
    openid = record.get("openid")
    text = record.get("text")
    timestamp = record.get("time")
    if (
        not isinstance(openid, str)
        or not openid.strip()
        or not isinstance(text, str)
        or not _is_int(timestamp)
        or timestamp < 0
    ):
        raise DatasetSyncError("dataset_record_invalid", "An inbound history record is invalid.")
    try:
        occurred_at = datetime.fromtimestamp(timestamp, timezone.utc)
        record_key = make_record_key(record, hash_key)
    except (OverflowError, OSError, ValueError) as exc:
        raise DatasetSyncError(
            "dataset_record_invalid", "An inbound history record has an invalid timestamp.", cause=exc
        ) from None
    return HistoryQuestion(record_key, text, occurred_at)


def sync_history(
    client: WeChatHistoryClient,
    store: CsvDatasetStore,
    *,
    start_date: date,
    end_date: date,
    reset: bool = False,
) -> SyncStats:
    if (
        not _date_value(start_date)
        or not _date_value(end_date)
        or start_date > end_date
    ):
        raise DatasetSyncError("dataset_range_invalid", "The requested date range is invalid.")
    now_china = _now_china()
    current_date = now_china.date()
    if end_date > current_date:
        raise DatasetSyncError(
            "dataset_range_invalid", "The end date cannot be later than today in China."
        )
    hash_key = os.environ.get("WECHAT_DATASET_HASH_KEY")
    if not isinstance(hash_key, str) or not hash_key.strip():
        raise DatasetSyncError(
            "dataset_hash_key_missing", "WECHAT_DATASET_HASH_KEY is required."
        )

    pages_fetched = 0
    inbound_records = 0
    inserted_rows = 0
    with store.locked():
        store.initialize()
        try:
            cursor = store.load_cursor(
                range_start_date=start_date,
                range_end_date=end_date,
            )
        except DatasetSyncError as exc:
            if exc.code != "dataset_range_mismatch" or not reset:
                raise
            cursor = None
        if cursor is None:
            next_date = start_date
            next_msgid = 1
        else:
            next_date = cursor.next_date
            next_msgid = cursor.next_msgid

        while next_date <= end_date:
            day_start = datetime.combine(next_date, day_time.min, tzinfo=CHINA_TZ)
            if next_date == current_date:
                day_end = now_china
            else:
                day_end = datetime.combine(
                    next_date + timedelta(days=1), day_time.min, tzinfo=CHINA_TZ
                ) - timedelta(seconds=1)
            page = client.fetch_page(
                start_time=int(day_start.timestamp()),
                end_time=int(day_end.timestamp()),
                msgid=next_msgid,
                number=PAGE_SIZE,
            )
            page_questions: list[HistoryQuestion] = []
            page_inbound = 0
            for record in page.records:
                question = _question_from_record(record, hash_key)
                if question is not None:
                    page_inbound += 1
                    page_questions.append(question)

            full_page = len(page.records) == PAGE_SIZE
            if full_page:
                next_cursor = SyncCursor(start_date, end_date, next_date, page.next_msgid)
            else:
                after_date = next_date if next_date == current_date else next_date + timedelta(days=1)
                next_cursor = SyncCursor(start_date, end_date, after_date, 1)
            inserted_rows += store.append_page(
                questions=page_questions,
                next_cursor=next_cursor,
            )
            pages_fetched += 1
            inbound_records += page_inbound
            if full_page:
                next_msgid = page.next_msgid
            elif next_date == current_date:
                break
            else:
                next_date += timedelta(days=1)
                next_msgid = 1

    return SyncStats(pages_fetched, inbound_records, inserted_rows)
