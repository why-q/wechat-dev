import contextlib
import csv
import hashlib
import hmac
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from rag_poc import cli
from rag_poc.dataset_sync import (
    CSV_HEADER,
    PAGE_SIZE,
    CsvDatasetStore,
    DatasetSyncError,
    HistoryPage,
    HistoryQuestion,
    SyncCursor,
    WeChatHistoryClient,
    make_record_key,
    sync_history,
)

CHINA = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 11, 23, 14, 30, 45, tzinfo=CHINA)


class _Response:
    def __init__(self, value):
        self.payload = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


class _FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def fetch_page(self, **kwargs):
        self.requests.append(kwargs)
        if not self.pages:
            raise AssertionError("unexpected extra page request")
        return self.pages.pop(0)


class HistoryClientTests(unittest.TestCase):
    def _client(self):
        return WeChatHistoryClient("fake-app", "fake-secret")

    @staticmethod
    def _token():
        return {"errcode": 0, "access_token": "fake-token", "expires_in": 3600}

    def test_short_page_uses_recordlist_length_not_response_number(self):
        with patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response(self._token()),
                _Response(
                    {
                        "errcode": 0,
                        "number": 10_000,
                        "recordlist": [{"opercode": 2003}],
                        "msgid": 9,
                    }
                ),
            ],
        ) as request:
            page = self._client().fetch_page(
                start_time=1, end_time=2, msgid=1, number=PAGE_SIZE
            )

        self.assertEqual(len(page.records), 1)
        self.assertEqual(page.number, 10_000)
        self.assertEqual(page.next_msgid, 1)
        payload = json.loads(request.call_args_list[1].args[0].data)
        self.assertEqual(payload, {"starttime": 1, "endtime": 2, "msgid": 1, "number": PAGE_SIZE})

    def test_full_page_requires_and_returns_changed_cursor(self):
        records = [{"opercode": 2002}] * PAGE_SIZE
        with patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response(self._token()),
                _Response({"errcode": 0, "number": 1, "recordlist": records, "msgid": 7}),
            ],
        ):
            page = self._client().fetch_page(
                start_time=1, end_time=2, msgid=1, number=PAGE_SIZE
            )
        self.assertEqual(len(page.records), PAGE_SIZE)
        self.assertEqual(page.next_msgid, 7)

        first = HistoryPage(records=records, number=PAGE_SIZE, next_msgid=7)
        second = HistoryPage(records=[], number=PAGE_SIZE, next_msgid=7)
        fake = _FakeClient([first, second])
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"WECHAT_DATASET_HASH_KEY": "stable-test-key"}, clear=False
        ), patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW):
            stats = sync_history(
                fake,
                CsvDatasetStore(Path(directory) / "dataset"),
                start_date=date(2026, 11, 22),
                end_date=date(2026, 11, 22),
            )
        self.assertEqual(stats.pages_fetched, 2)
        self.assertEqual([request["msgid"] for request in fake.requests], [1, 7])

    def test_short_page_allows_omitted_msgid_and_rejects_bool_number(self):
        with patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response(self._token()),
                _Response({"errcode": 0, "number": 10_000, "recordlist": []}),
            ],
        ):
            page = self._client().fetch_page(start_time=1, end_time=2, msgid=4)
        self.assertEqual(page.next_msgid, 4)
        with patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response(self._token()),
                _Response({"errcode": 0, "number": 0, "recordlist": [], "msgid": 0}),
            ],
        ):
            page = self._client().fetch_page(start_time=1, end_time=2, msgid=4)
        self.assertEqual(page.next_msgid, 4)

        with patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response(self._token()),
                _Response({"errcode": 0, "number": True, "recordlist": []}),
            ],
        ):
            with self.assertRaises(DatasetSyncError) as caught:
                self._client().fetch_page(start_time=1, end_time=2, msgid=1)
        self.assertEqual(caught.exception.code, "wechat_response_invalid")

    def test_access_token_error_retries_same_page_once_with_fresh_token(self):
        page_response = {
            "errcode": 0,
            "number": 0,
            "recordlist": [],
        }
        responses = [
            _Response(self._token()),
            _Response({"errcode": 40001, "errmsg": "not printed"}),
            _Response({"errcode": 0, "access_token": "new-token", "expires_in": 3600}),
            _Response(page_response),
        ]
        with patch("rag_poc.dataset_sync.urlopen", side_effect=responses) as request:
            page = self._client().fetch_page(start_time=1, end_time=2, msgid=1)
        self.assertEqual(page.records, [])
        self.assertEqual(request.call_count, 4)
        first_page = request.call_args_list[1].args[0]
        retry_page = request.call_args_list[3].args[0]
        self.assertEqual(json.loads(first_page.data), json.loads(retry_page.data))
        self.assertIn("fake-token", first_page.full_url)
        self.assertIn("new-token", retry_page.full_url)

    def test_errors_do_not_expose_upstream_body_or_credentials(self):
        client = self._client()
        with patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response(self._token()),
                _Response({"errcode": 65416, "errmsg": "private response text"}),
            ],
        ):
            with self.assertRaises(DatasetSyncError) as caught:
                client.fetch_page(start_time=1, end_time=2, msgid=1)
        message = str(caught.exception)
        self.assertNotIn("private response text", message)
        self.assertNotIn("fake-secret", message)
        self.assertEqual(caught.exception.api_errcode, 65416)


class CsvDatasetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temporary.name) / "private-data"
        self.store = CsvDatasetStore(self.output_dir)
        self.cursor = SyncCursor(
            date(2026, 11, 20), date(2026, 11, 22), date(2026, 11, 21), 3
        )
        self.question = HistoryQuestion(
            "a" * 64,
            "开放时间, 周末？\n[image]",
            datetime(2026, 11, 21, 4, 5, 6, tzinfo=timezone.utc),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_csv_contract_bom_quoting_permissions_and_deduplication(self):
        with self.store.locked():
            self.store.initialize()
            first = self.store.append_page(questions=[self.question], next_cursor=self.cursor)
            repeated = self.store.append_page(questions=[self.question], next_cursor=self.cursor)

        raw = self.store.csv_path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        with self.store.csv_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, list(CSV_HEADER))
            rows = list(reader)
        self.assertEqual(first, 1)
        self.assertEqual(repeated, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], self.question.text)
        self.assertEqual(rows[0]["source"], "getmsglist")
        self.assertNotIn("openid", rows[0])
        self.assertIn(b'"\xe5\xbc\x80\xe6\x94\xbe\xe6\x97\xb6\xe9\x97\xb4,', raw)
        self.assertEqual(stat.S_IMODE(self.output_dir.stat().st_mode), 0o700)
        for path in (self.store.csv_path, self.store.state_path, self.store.lock_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        state = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(state),
            {"range_start_date", "range_end_date", "next_date", "next_msgid", "updated_at_utc"},
        )
        self.assertNotIn(self.question.text, json.dumps(state))

    def test_hmac_uses_only_canonical_fields_and_default_worker(self):
        record = {
            "openid": "private-openid",
            "opercode": 2003,
            "text": "问题 [voice]",
            "time": 10,
            "unrelated": "ignored",
        }
        expected_payload = json.dumps(
            {
                "openid": "private-openid",
                "opercode": 2003,
                "text": "问题 [voice]",
                "time": 10,
                "worker": "",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected = hmac.new(b"key", expected_payload, hashlib.sha256).hexdigest()
        self.assertEqual(make_record_key(record, "key"), expected)
        record["unrelated"] = "changed"
        self.assertEqual(make_record_key(record, "key"), expected)
        self.assertEqual(len(expected), 64)

    def test_valid_quoted_multiline_row_is_not_repaired(self):
        with self.store.locked():
            self.store.initialize()
            self.store.append_page(questions=[self.question], next_cursor=self.cursor)
        reader = CsvDatasetStore(self.output_dir)
        loaded = reader.load_cursor(
            range_start_date=self.cursor.range_start_date,
            range_end_date=self.cursor.range_end_date,
        )
        self.assertEqual(loaded, self.cursor)
        with self.store.csv_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["question"], self.question.text)

    def test_incomplete_terminal_physical_line_is_removed(self):
        with self.store.locked():
            self.store.initialize()
            self.store.append_page(questions=[self.question], next_cursor=self.cursor)
            complete_csv = self.store.csv_path.read_bytes()
            with self.store.csv_path.open("ab") as stream:
                stream.write(b"partial,2026-11-21T00:00:00Z,unfinished\r")
        CsvDatasetStore(self.output_dir).load_cursor(
            range_start_date=self.cursor.range_start_date,
            range_end_date=self.cursor.range_end_date,
        )
        self.assertEqual(self.store.csv_path.read_bytes(), complete_csv)

    def test_torn_multiline_utf8_tail_preserves_earlier_rows(self):
        with self.store.locked():
            self.store.initialize()
            self.store.append_page(questions=[self.question], next_cursor=self.cursor)
            complete_csv = self.store.csv_path.read_bytes()
            with self.store.csv_path.open("ab") as stream:
                torn_tail = "second line ends in 中文".encode("utf-8")[:-1]
                stream.write(b'partial,2026-11-21T00:00:00Z,"first physical line\r\n')
                stream.write(torn_tail)
        CsvDatasetStore(self.output_dir).load_cursor(
            range_start_date=self.cursor.range_start_date,
            range_end_date=self.cursor.range_end_date,
        )
        self.assertEqual(self.store.csv_path.read_bytes(), complete_csv)

    def test_malformed_earlier_row_with_trailing_torn_line_fails_closed(self):
        self.store.initialize()
        original = (
            b"\xef\xbb\xbfrecord_key,message_time_utc,question,source\r\n"
            b"malformed,too,few\r\n"
            b"torn,physical,line"
        )
        self.store.csv_path.write_bytes(original)
        with self.assertRaises(DatasetSyncError) as caught:
            self.store.load_cursor(
                range_start_date=self.cursor.range_start_date,
                range_end_date=self.cursor.range_end_date,
            )
        self.assertEqual(caught.exception.code, "dataset_csv_invalid")
        self.assertEqual(self.store.csv_path.read_bytes(), original)

    def test_existing_rows_require_sha256_keys_and_utc_timestamps(self):
        header = b"\xef\xbb\xbfrecord_key,message_time_utc,question,source\r\n"
        for row in (
            b"not-a-digest,2026-11-21T00:00:00Z,text,getmsglist\r\n",
            b"" + b"a" * 64 + b",not-a-time,text,getmsglist\r\n",
        ):
            with self.subTest(row=row):
                self.store.initialize()
                self.store.csv_path.write_bytes(header + row)
                with self.assertRaises(DatasetSyncError):
                    self.store.load_cursor(
                        range_start_date=self.cursor.range_start_date,
                        range_end_date=self.cursor.range_end_date,
                    )

    def test_state_failure_after_csv_fsync_is_safe_to_retry(self):
        with self.store.locked():
            self.store.initialize()
            with patch("rag_poc.dataset_sync.os.replace", side_effect=OSError("not shown")):
                with self.assertRaises(DatasetSyncError) as caught:
                    self.store.append_page(questions=[self.question], next_cursor=self.cursor)
            self.assertEqual(caught.exception.code, "dataset_state_write_failed")
            with self.store.csv_path.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)

        retry_store = CsvDatasetStore(self.output_dir)
        with retry_store.locked():
            retry_store.initialize()
            inserted = retry_store.append_page(questions=[self.question], next_cursor=self.cursor)
            self.assertEqual(inserted, 0)
            loaded = retry_store.load_cursor(
                range_start_date=self.cursor.range_start_date,
                range_end_date=self.cursor.range_end_date,
            )
        self.assertEqual(loaded, self.cursor)
        with retry_store.csv_path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_lock_is_nonblocking(self):
        competing_store = CsvDatasetStore(self.output_dir)
        with self.store.locked():
            self.store.initialize()
            with self.assertRaises(DatasetSyncError) as caught:
                with competing_store.locked():
                    self.fail("a second lock must not be acquired")
        self.assertEqual(caught.exception.code, "dataset_lock_unavailable")

    def test_existing_public_directory_is_rejected_without_chmod(self):
        public_dir = Path(self.temporary.name) / "existing-public"
        public_dir.mkdir(mode=0o755)
        public_dir.chmod(0o755)
        store = CsvDatasetStore(public_dir)
        before = stat.S_IMODE(public_dir.stat().st_mode)
        with self.assertRaises(DatasetSyncError) as caught:
            with store.locked():
                self.fail("public directory should be rejected before locking")
        self.assertEqual(caught.exception.code, "dataset_directory_not_private")
        self.assertEqual(stat.S_IMODE(public_dir.stat().st_mode), before)

    def test_unterminated_final_physical_line_is_not_repaired_if_terminated(self):
        self.store.initialize()
        with self.store.csv_path.open("ab") as stream:
            stream.write(b'partial,2026-11-21T00:00:00Z,"unclosed\r\n')
        before = self.store.csv_path.read_bytes()
        with self.assertRaises(DatasetSyncError):
            self.store.load_cursor(
                range_start_date=self.cursor.range_start_date,
                range_end_date=self.cursor.range_end_date,
            )
        self.assertEqual(self.store.csv_path.read_bytes(), before)


class SyncHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temporary.name) / "data"
        self.store = CsvDatasetStore(self.output_dir)
        self.env = patch.dict(
            os.environ, {"WECHAT_DATASET_HASH_KEY": "collector-test-key"}, clear=False
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def test_completed_china_days_have_midnight_to_midnight_boundaries(self):
        fake = _FakeClient(
            [
                HistoryPage([], 10_000, 1),
                HistoryPage([], 10_000, 1),
            ]
        )
        start = date(2026, 11, 21)
        end = date(2026, 11, 22)
        with patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW):
            stats = sync_history(fake, self.store, start_date=start, end_date=end)
        self.assertEqual(stats.pages_fetched, 2)
        boundaries = [
            (
                datetime.fromtimestamp(request["start_time"], CHINA),
                datetime.fromtimestamp(request["end_time"], CHINA),
            )
            for request in fake.requests
        ]
        self.assertEqual(boundaries[0][0], datetime(2026, 11, 21, 0, 0, tzinfo=CHINA))
        self.assertEqual(boundaries[0][1], datetime(2026, 11, 21, 23, 59, 59, tzinfo=CHINA))
        self.assertEqual(boundaries[1][0], datetime(2026, 11, 22, 0, 0, tzinfo=CHINA))
        self.assertEqual(boundaries[1][1], datetime(2026, 11, 22, 23, 59, 59, tzinfo=CHINA))
        for day_start, day_end in boundaries:
            self.assertEqual(day_start.date(), day_end.date())
        cursor = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["next_date"], "2026-11-23")

    def test_current_day_short_page_keeps_cursor_on_today(self):
        record = {"openid": "private", "opercode": 2003, "text": "开放时间？", "time": 1}
        fake = _FakeClient([HistoryPage([record], 10_000, 1)])
        with patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW):
            stats = sync_history(
                fake,
                self.store,
                start_date=FIXED_NOW.date(),
                end_date=FIXED_NOW.date(),
            )
        request = fake.requests[0]
        self.assertEqual(
            datetime.fromtimestamp(request["start_time"], CHINA),
            datetime(2026, 11, 23, 0, 0, tzinfo=CHINA),
        )
        self.assertEqual(
            datetime.fromtimestamp(request["end_time"], CHINA), FIXED_NOW.replace(microsecond=0)
        )
        state = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["next_date"], "2026-11-23")
        self.assertEqual(state["next_msgid"], 1)
        self.assertEqual(stats.inserted_rows, 1)

    def test_inbound_record_validation_precedes_csv_and_cursor_changes(self):
        malformed_records = (
            {"openid": "private", "opercode": 2003, "text": 1, "time": 1},
            {"openid": "private", "opercode": True, "text": "Q?", "time": 1},
            {"openid": "private", "opercode": 2003, "text": "Q?", "time": True},
        )
        with patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW):
            for record in malformed_records:
                with self.subTest(record=record), self.assertRaises(DatasetSyncError) as caught:
                    sync_history(
                        _FakeClient([HistoryPage([record], 1, 1)]),
                        self.store,
                        start_date=date(2026, 11, 22),
                        end_date=date(2026, 11, 22),
                    )
                self.assertEqual(caught.exception.code, "dataset_record_invalid")
        self.assertFalse(self.store.state_path.exists())
        with self.store.csv_path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def test_lone_surrogate_in_inbound_text_is_reported_safely(self):
        page = {
            "errcode": 0,
            "number": 10_000,
            "recordlist": [
                {"openid": "private", "opercode": 2003, "text": "bad\ud800text", "time": 1}
            ],
            "msgid": 1,
        }
        with patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW), patch(
            "rag_poc.dataset_sync.urlopen",
            side_effect=[
                _Response({"errcode": 0, "access_token": "fake-token", "expires_in": 3600}),
                _Response(page),
            ],
        ):
            with self.assertRaises(DatasetSyncError) as caught:
                sync_history(
                    WeChatHistoryClient("fake-app", "fake-secret"),
                    self.store,
                    start_date=date(2026, 11, 22),
                    end_date=date(2026, 11, 22),
                )
        self.assertEqual(caught.exception.code, "dataset_record_invalid")
        self.assertNotIn("bad", str(caught.exception))
        self.assertFalse(self.store.state_path.exists())

    def test_malformed_csv_fails_before_client_contact(self):
        self.store.initialize()
        self.store.csv_path.write_bytes(b"wrong,header\r\n")
        fake = _FakeClient([])
        with patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW):
            with self.assertRaises(DatasetSyncError):
                sync_history(
                    fake,
                    self.store,
                    start_date=date(2026, 11, 22),
                    end_date=date(2026, 11, 22),
                )
        self.assertEqual(fake.requests, [])

    def test_range_reset_preserves_csv_and_restarts_at_first_cursor(self):
        record = {"openid": "private", "opercode": 2003, "text": "Q?", "time": 1}
        old_range = (date(2026, 11, 21), date(2026, 11, 21))
        with patch("rag_poc.dataset_sync._now_china", return_value=FIXED_NOW):
            sync_history(
                _FakeClient([HistoryPage([record], 1, 1)]),
                self.store,
                start_date=old_range[0],
                end_date=old_range[1],
            )
            with self.assertRaises(DatasetSyncError) as caught:
                sync_history(
                    _FakeClient([]),
                    self.store,
                    start_date=date(2026, 11, 22),
                    end_date=date(2026, 11, 22),
                )
            self.assertEqual(caught.exception.code, "dataset_range_mismatch")
            fake = _FakeClient([HistoryPage([record], 1, 1)])
            stats = sync_history(
                fake,
                self.store,
                start_date=date(2026, 11, 22),
                end_date=date(2026, 11, 22),
                reset=True,
            )
        self.assertEqual(fake.requests[0]["msgid"], 1)
        self.assertEqual(stats.inserted_rows, 0)


class DatasetCliTests(unittest.TestCase):
    def test_dataset_dates_require_exact_calendar_format(self):
        parser = cli.create_parser()
        common = ["dataset-sync", "--end-date", "2026-11-23", "--output-dir", "/tmp/private"]
        self.assertEqual(
            parser.parse_args([*common[:1], "--start-date", "2026-11-01", *common[1:]]).start_date,
            date(2026, 11, 1),
        )
        for value in ("20261101", "2026-W44-7", "2026-02-30"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(
                        ["dataset-sync", "--start-date", value, "--end-date", "2026-11-23", "--output-dir", "/tmp/private"]
                    )

    def test_api_error_prints_wechat_errcode_without_payload(self):
        with patch.dict(os.environ, {"WECHAT_APP_ID": "id", "WECHAT_APP_SECRET": "secret"}, clear=True), patch(
            "rag_poc.cli.load_dotenv"
        ), patch("rag_poc.dataset_sync.sync_history", side_effect=DatasetSyncError(
            "wechat_api_error", "The WeChat API rejected the request.", api_errcode=45011
        )), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            status = cli.main(["dataset-sync", "--start-date", "2026-11-23", "--end-date", "2026-11-23", "--output-dir", "/tmp/private"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("wechat_errcode=45011", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_main_collects_and_reruns_without_duplicate_rows(self):
        record = {
            "openid": "test-openid-only-in-memory",
            "opercode": 2003,
            "text": "开放时间？ [image]",
            "time": int(FIXED_NOW.timestamp()),
        }
        page = {"errcode": 0, "number": 10_000, "recordlist": [record], "msgid": 11}
        responses = [
            _Response({"errcode": 0, "access_token": "token-one", "expires_in": 3600}),
            _Response(page),
            _Response({"errcode": 0, "access_token": "token-two", "expires_in": 3600}),
            _Response(page),
        ]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(
            os.environ,
            {
                "WECHAT_APP_ID": "fake-app-id",
                "WECHAT_APP_SECRET": "fake-app-secret",
                "WECHAT_DATASET_HASH_KEY": "fake-test-hash-key",
            },
            clear=True,
        ), patch("rag_poc.cli.load_dotenv"), patch(
            "rag_poc.dataset_sync._now_china", return_value=FIXED_NOW
        ), patch("rag_poc.dataset_sync.urlopen", side_effect=responses) as request, contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            output_dir = Path(temporary.name) / "dataset"
            arguments = [
                "dataset-sync",
                "--start-date",
                "2026-11-23",
                "--end-date",
                "2026-11-23",
                "--output-dir",
                str(output_dir),
            ]
            self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(cli.main(arguments), 0)

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "dataset-sync: pages_fetched=1 inserted_rows=1",
                "dataset-sync: pages_fetched=1 inserted_rows=0",
            ],
        )
        self.assertEqual(request.call_count, 4)
        payloads = [json.loads(call.args[0].data) for call in request.call_args_list if call.args[0].data]
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0]["msgid"], 1)
        self.assertEqual(payloads[0]["number"], PAGE_SIZE)
        with (output_dir / "questions.csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, list(CSV_HEADER))
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], record["text"])
        self.assertEqual(rows[0]["source"], "getmsglist")
        self.assertNotIn("openid", rows[0])
        state = json.loads((output_dir / "sync-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["next_date"], "2026-11-23")
        self.assertEqual(state["next_msgid"], 1)


if __name__ == "__main__":
    unittest.main()
