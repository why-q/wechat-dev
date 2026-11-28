"""SQLite WAL persistence layer for WeChat library support intake, merging, and staff cases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from .auth import (
    ALL_LIBRARIAN_PERMISSIONS,
    DEFAULT_LIBRARIAN_PERMISSIONS,
    VALID_ROLES,
    DashboardPrincipal,
    generate_session_token,
    hash_password,
    hash_session_token,
    normalize_permissions,
    valid_username,
    verify_password,
)
from .filters import FilterDecision
from .schema import (
    SCHEMA_SQL as _SCHEMA_SQL,
    SCHEMA_VERSION,
    BatchSnapshot,
    ClarificationSnapshot,
    FlushOutcome,
    IngestResult,
    StaffCaseSnapshot,
    coerce_headers_json as _coerce_headers_json,
    coerce_msg_id as _coerce_msg_id,
    coerce_str as _coerce_str,
    default_same_case_adapter as _default_same_case_adapter,
    utc_iso as _utc_iso,
)

DEFAULT_QUIET_SECONDS = 3.0
DEFAULT_MAX_WAIT_SECONDS = 10.0
DEFAULT_MERGE_ENABLED = False
DEFAULT_MAX_PREEMPTS = 2
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600
DEFAULT_CLARIFICATION_TTL_SECONDS = 1800.0
WECHAT_INTERACTION_WINDOW_SECONDS = 48 * 3600
WECHAT_INTERACTION_QUOTA_LIMIT = 5

_UTC8 = timezone(timedelta(hours=8))
_WEEKDAY_ZH = ("一", "二", "三", "四", "五", "六", "日")
_PURGE_CHUNK_SIZE = 500
_ROUTING_TIER1_PRESET = frozenset({"exact_alias", "decision_preset"})
_ROUTING_TIER2_RAG = frozenset({"tier2_short", "tier2_long"})
_ROUTING_CLARIFY = frozenset({"decision_clarify", "tier2_clarify"})
_ROUTING_STAFF_FALLBACK = frozenset(
    {
        "decision_staff",
        "tier2_staff",
        "clarify_limit_fallback",
        "tier2_unavailable_fallback",
        "tier2_error_fallback",
        "tier2_fallback",
        "decision_unknown_fallback",
        "staff_case_appended",
    }
)


def _epoch_to_utc8(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(float(epoch_seconds), tz=_UTC8)


def _parse_closed_epoch(closed_at_utc: str | None, updated_at_epoch: float) -> float:
    if isinstance(closed_at_utc, str) and closed_at_utc.strip():
        raw = closed_at_utc.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    return float(updated_at_epoch)

SameCaseMatcher = Callable[[str, dict[str, Any], float], tuple[bool, str]]


class SupportStore:
    """Thread-safe SQLite store managing inbound events, merge windows, feedbacks, and staff cases."""

    def __init__(
        self,
        db_path: Path,
        *,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        merge_enabled: bool = DEFAULT_MERGE_ENABLED,
        max_preempts: int = DEFAULT_MAX_PREEMPTS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
        same_case_matcher: SameCaseMatcher = _default_same_case_adapter,
    ) -> None:
        if quiet_seconds <= 0:
            raise ValueError("quiet_seconds must be positive")
        if max_wait_seconds < quiet_seconds:
            raise ValueError("max_wait_seconds must be >= quiet_seconds")
        if max_preempts < 0:
            raise ValueError("max_preempts must be >= 0")
        self.db_path = Path(db_path)
        self.quiet_seconds = float(quiet_seconds)
        self.max_wait_seconds = float(max_wait_seconds)
        self.merge_enabled = bool(merge_enabled)
        self.max_preempts = int(max_preempts)
        self.retention_seconds = float(retention_seconds)
        self._clock = clock
        self._same_case_matcher = same_case_matcher
        self._lock = threading.RLock()
        self._conn = self._open_connection()
        self._init_schema()
        self._enforce_permissions()

    def _open_connection(self) -> sqlite3.Connection:
        parent = self.db_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)
            existing_cols = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(merge_batches)").fetchall()
            }
            staff_cases_cols = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(staff_cases)").fetchall()
            }
            case_msg_cols = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(staff_case_messages)").fetchall()
            }
            outbound_cols = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(outbound_replies)").fetchall()
            }
            with self._conn:
                if "generation" not in existing_cols:
                    self._conn.execute(
                        "ALTER TABLE merge_batches ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
                    )
                if "preempt_count" not in existing_cols:
                    self._conn.execute(
                        "ALTER TABLE merge_batches ADD COLUMN preempt_count INTEGER NOT NULL DEFAULT 0"
                    )
                if "preempt_locked" not in existing_cols:
                    self._conn.execute(
                        "ALTER TABLE merge_batches ADD COLUMN preempt_locked INTEGER NOT NULL DEFAULT 0"
                    )
                if "closed_by" not in staff_cases_cols:
                    self._conn.execute(
                        "ALTER TABLE staff_cases ADD COLUMN closed_by TEXT NOT NULL DEFAULT ''"
                    )
                if "operator_username" not in case_msg_cols:
                    self._conn.execute(
                        "ALTER TABLE staff_case_messages ADD COLUMN operator_username TEXT NOT NULL DEFAULT ''"
                    )
                if "operator_username" not in outbound_cols:
                    self._conn.execute(
                        "ALTER TABLE outbound_replies ADD COLUMN operator_username TEXT NOT NULL DEFAULT ''"
                    )
                self._conn.execute("DROP INDEX IF EXISTS idx_merge_open_reader;")
                self._conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_merge_active_reader
                    ON merge_batches(account_id, reader_id)
                    WHERE status IN ('open', 'processing') AND preempt_locked = 0;
                    """
                )
                self._conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_merge_queued_reader
                    ON merge_batches(account_id, reader_id)
                    WHERE status = 'queued';
                    """
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def _enforce_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.db_path}{suffix}")
            if target.exists():
                os.chmod(target, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def now(self) -> float:
        return float(self._clock())

    def _row_to_batch(self, row: sqlite3.Row) -> BatchSnapshot:
        return BatchSnapshot(
            batch_id=int(row["batch_id"]),
            account_id=str(row["account_id"]),
            reader_id=str(row["reader_id"]),
            status=str(row["status"]),
            first_received_at_epoch=float(row["first_received_at_epoch"]),
            last_received_at_epoch=float(row["last_received_at_epoch"]),
            quiet_deadline_epoch=float(row["quiet_deadline_epoch"]),
            max_deadline_epoch=float(row["max_deadline_epoch"]),
            flush_at_epoch=float(row["flush_at_epoch"]),
            message_count=int(row["message_count"]),
            merged_text=row["merged_text"],
            flushed_at_epoch=float(row["flushed_at_epoch"]) if row["flushed_at_epoch"] is not None else None,
            route_stage=str(row["route_stage"]),
            staff_case_id=int(row["staff_case_id"]) if row["staff_case_id"] is not None else None,
            generation=int(row["generation"]),
            preempt_count=int(row["preempt_count"]),
            preempt_locked=bool(row["preempt_locked"]),
        )

    def _row_to_case(self, row: sqlite3.Row) -> StaffCaseSnapshot:
        keys = row.keys()
        closed_by = str(row["closed_by"]) if "closed_by" in keys and row["closed_by"] is not None else ""
        return StaffCaseSnapshot(
            case_id=int(row["case_id"]),
            account_id=str(row["account_id"]),
            reader_id=str(row["reader_id"]),
            case_kind=str(row["case_kind"]),
            status=str(row["status"]),
            summary=str(row["summary"]),
            latest_message=str(row["latest_message"]),
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
            updated_at_epoch=float(row["updated_at_epoch"]),
            closed_at_utc=row["closed_at_utc"],
            resolution_note=str(row["resolution_note"]),
            closed_by=closed_by,
        )

    def ingest_event(
        self,
        payload: dict[str, Any],
        decision: FilterDecision,
        *,
        client_ip: str = "",
        received_at_epoch: float | None = None,
    ) -> IngestResult:
        """Persist an inbound event atomically, deduplicate by (account, MsgId), and route by Filter."""
        account_id = _coerce_str(payload.get("to_user")).strip() or "default"
        reader_id = _coerce_str(payload.get("from_user")).strip()
        msg_type = _coerce_str(payload.get("msg_type")).strip()
        event_name = _coerce_str(payload.get("event")).strip()
        msg_id = _coerce_msg_id(payload.get("msg_id"))
        dedup_key = f"msg:{account_id}:{msg_id}" if msg_id else None

        with self._lock:
            now_epoch = float(self._clock() if received_at_epoch is None else received_at_epoch)
            if dedup_key:
                existing = self._conn.execute(
                    """
                    SELECT seq, filter_category, filter_reason, content, batch_id, staff_case_id, feedback_id
                    FROM inbound_events
                    WHERE dedup_key = ?
                    """,
                    (dedup_key,),
                ).fetchone()
                if existing is not None:
                    existing_batch = (
                        self.get_batch(int(existing["batch_id"]))
                        if existing["batch_id"] is not None
                        else None
                    )
                    return IngestResult(
                        duplicate=True,
                        seq=int(existing["seq"]),
                        received_at_epoch=now_epoch,
                        filter_decision=FilterDecision(
                            category=str(existing["filter_category"]),
                            reason=str(existing["filter_reason"]),
                            normalized_text=str(existing["content"]),
                        ),
                        batch=existing_batch,
                        feedback_id=int(existing["feedback_id"]) if existing["feedback_id"] is not None else None,
                        staff_case_id=(
                            int(existing["staff_case_id"]) if existing["staff_case_id"] is not None else None
                        ),
                    )

            pre_flushed: list[FlushOutcome] = []
            if decision.category == "question" and reader_id and self.merge_enabled:
                open_row = self._conn.execute(
                    """
                    SELECT * FROM merge_batches
                    WHERE account_id = ? AND reader_id = ? AND status = 'open'
                    """,
                    (account_id, reader_id),
                ).fetchone()
                if open_row is not None and now_epoch >= float(open_row["flush_at_epoch"]):
                    outcome = self._flush_batch_locked(int(open_row["batch_id"]), now_epoch=now_epoch)
                    if outcome is not None:
                        pre_flushed.append(outcome)

            now_utc = _utc_iso(now_epoch)
            expires_at = now_epoch + self.retention_seconds if self.retention_seconds > 0 else None
            content_raw = _coerce_str(payload.get("content"))

            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO inbound_events(
                        dedup_key, transport, received_at_epoch, received_at_utc, create_time,
                        account_id, reader_id, msg_type, event, msg_id, content,
                        filter_category, filter_reason, client_ip, wx_headers_json, raw_body,
                        expires_at_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dedup_key,
                        _coerce_str(payload.get("transport")),
                        now_epoch,
                        now_utc,
                        _coerce_str(payload.get("create_time")),
                        account_id,
                        reader_id,
                        msg_type,
                        event_name,
                        msg_id or None,
                        content_raw,
                        decision.category,
                        decision.reason,
                        client_ip,
                        _coerce_headers_json(payload.get("wx_headers")),
                        _coerce_str(payload.get("raw_body")),
                        expires_at,
                    ),
                )
                seq = int(cursor.lastrowid)

                batch_snapshot: BatchSnapshot | None = None
                feedback_id: int | None = None
                staff_case_id: int | None = None
                preempted_batch_id: int | None = None
                queued_behind_batch_id: int | None = None
                allowlist_enrolled: bool | None = None

                if decision.category == "question" and reader_id:
                    if self.merge_enabled:
                        batch_id = self._enqueue_debounce_locked(
                            account_id=account_id,
                            reader_id=reader_id,
                            seq=seq,
                            text_content=decision.normalized_text,
                            now_epoch=now_epoch,
                        )
                    else:
                        batch_id, preempted_batch_id, queued_behind_batch_id = (
                            self._enqueue_immediate_locked(
                                account_id=account_id,
                                reader_id=reader_id,
                                seq=seq,
                                text_content=decision.normalized_text,
                                now_epoch=now_epoch,
                            )
                        )
                    batch_snapshot = self.get_batch(batch_id)

                elif decision.category == "feedback":
                    feedback_id = self._record_feedback_locked(
                        account_id=account_id,
                        reader_id=reader_id,
                        seq=seq,
                        content=decision.normalized_text,
                        now_epoch=now_epoch,
                        now_utc=now_utc,
                    )

                elif decision.category == "emergency":
                    staff_case_id = self._record_emergency_locked(
                        account_id=account_id,
                        reader_id=reader_id,
                        seq=seq,
                        content=decision.normalized_text,
                        now_epoch=now_epoch,
                        now_utc=now_utc,
                    )

                elif decision.category == "gray_enroll" and reader_id:
                    allowlist_enrolled = self._enroll_allowlist_locked(
                        account_id=account_id,
                        reader_id=reader_id,
                        source="keyword",
                        now_epoch=now_epoch,
                        now_utc=now_utc,
                    )

            self._enforce_permissions()
            return IngestResult(
                duplicate=False,
                seq=seq,
                received_at_epoch=now_epoch,
                filter_decision=decision,
                batch=batch_snapshot,
                feedback_id=feedback_id,
                staff_case_id=staff_case_id,
                pre_flushed=tuple(pre_flushed),
                preempted_batch_id=preempted_batch_id,
                queued_behind_batch_id=queued_behind_batch_id,
                allowlist_enrolled=allowlist_enrolled,
            )

    def _record_feedback_locked(
        self,
        *,
        account_id: str,
        reader_id: str,
        seq: int,
        content: str,
        now_epoch: float,
        now_utc: str,
    ) -> int:
        fb_cursor = self._conn.execute(
            """
            INSERT INTO feedback_records(
                account_id, reader_id, event_seq, created_at_epoch, created_at_utc, content
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (account_id, reader_id, seq, now_epoch, now_utc, content),
        )
        feedback_id = int(fb_cursor.lastrowid)
        self._conn.execute(
            "UPDATE inbound_events SET feedback_id = ? WHERE seq = ?",
            (feedback_id, seq),
        )
        return feedback_id

    def _record_emergency_locked(
        self,
        *,
        account_id: str,
        reader_id: str,
        seq: int,
        content: str,
        now_epoch: float,
        now_utc: str,
    ) -> int:
        case_cursor = self._conn.execute(
            """
            INSERT INTO staff_cases(
                account_id, reader_id, source_event_seq, case_kind, status,
                summary, latest_message, created_at_epoch, created_at_utc,
                updated_at_epoch, updated_at_utc
            ) VALUES (?, ?, ?, 'emergency', 'open', ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                reader_id,
                seq,
                content[:120],
                content,
                now_epoch,
                now_utc,
                now_epoch,
                now_utc,
            ),
        )
        staff_case_id = int(case_cursor.lastrowid)
        self._conn.execute(
            """
            INSERT INTO staff_case_messages(
                case_id, event_seq, sender_role, content, created_at_epoch, created_at_utc
            ) VALUES (?, ?, 'reader', ?, ?, ?)
            """,
            (staff_case_id, seq, content, now_epoch, now_utc),
        )
        self._conn.execute(
            "UPDATE inbound_events SET staff_case_id = ? WHERE seq = ?",
            (staff_case_id, seq),
        )
        return staff_case_id

    def _enroll_allowlist_locked(
        self,
        *,
        account_id: str,
        reader_id: str,
        source: str,
        now_epoch: float,
        now_utc: str,
    ) -> bool:
        acct = account_id or "default"
        existing = self._conn.execute(
            "SELECT 1 FROM auto_reply_allowlist WHERE account_id = ? AND reader_id = ?",
            (acct, reader_id),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO auto_reply_allowlist(
                    account_id, reader_id, source,
                    created_at_epoch, created_at_utc, updated_at_epoch, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (acct, reader_id, source, now_epoch, now_utc, now_epoch, now_utc),
            )
            return True
        self._conn.execute(
            """
            UPDATE auto_reply_allowlist
            SET source = ?, updated_at_epoch = ?, updated_at_utc = ?
            WHERE account_id = ? AND reader_id = ?
            """,
            (source, now_epoch, now_utc, acct, reader_id),
        )
        return False

    def enroll_allowlist(
        self,
        *,
        account_id: str,
        reader_id: str,
        source: str = "keyword",
        now_epoch: float | None = None,
    ) -> bool:
        """Persist a dynamic allowlist reader and return True iff newly added."""
        clean_reader = reader_id.strip()
        if not clean_reader:
            raise ValueError("reader_id must be non-empty")
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                return self._enroll_allowlist_locked(
                    account_id=account_id.strip() or "default",
                    reader_id=clean_reader,
                    source=source,
                    now_epoch=now_val,
                    now_utc=now_utc,
                )

    def is_allowlisted(self, account_id: str, reader_id: str) -> bool:
        """Return whether `(account_id, reader_id)` is present in the dynamic allowlist."""
        if not reader_id:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM auto_reply_allowlist
                WHERE account_id = ? AND reader_id = ?
                LIMIT 1
                """,
                (account_id or "default", reader_id),
            ).fetchone()
            return row is not None

    def allowlist_reader_ids(self) -> frozenset[str]:
        """Return distinct reader IDs enrolled in the dynamic allowlist."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT reader_id FROM auto_reply_allowlist"
            ).fetchall()
            return frozenset(str(row["reader_id"]) for row in rows)

    def list_allowlist(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """List dynamic allowlist entries ordered by most recent enrollment/refresh."""
        bounded = max(1, min(limit, 200))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT account_id, reader_id, source, created_at_utc, updated_at_utc
                FROM auto_reply_allowlist
                ORDER BY updated_at_epoch DESC, reader_id ASC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            return [dict(row) for row in rows]

    def remove_allowlist(self, reader_id: str, *, account_id: str | None = None) -> int:
        """Remove a reader from the dynamic allowlist and return deleted row count."""
        clean_reader = reader_id.strip()
        if not clean_reader:
            return 0
        with self._lock:
            with self._conn:
                if account_id is not None and account_id.strip():
                    cursor = self._conn.execute(
                        "DELETE FROM auto_reply_allowlist WHERE account_id = ? AND reader_id = ?",
                        (account_id.strip(), clean_reader),
                    )
                else:
                    cursor = self._conn.execute(
                        "DELETE FROM auto_reply_allowlist WHERE reader_id = ?",
                        (clean_reader,),
                    )
                return int(cursor.rowcount)

    def clear_allowlist(self) -> int:
        """Remove all readers from the dynamic allowlist and return deleted row count."""
        with self._lock:
            with self._conn:
                cursor = self._conn.execute("DELETE FROM auto_reply_allowlist")
                return int(cursor.rowcount)

    def _append_batch_message_locked(
        self,
        *,
        batch_id: int,
        seq: int,
        now_epoch: float,
        text_content: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO batch_messages(batch_id, event_seq, received_at_epoch, text_content)
            VALUES (?, ?, ?, ?)
            """,
            (batch_id, seq, now_epoch, text_content),
        )
        self._conn.execute(
            "UPDATE inbound_events SET batch_id = ? WHERE seq = ?",
            (batch_id, seq),
        )

    def _ordered_batch_texts_locked(self, batch_id: int) -> tuple[tuple[int, ...], str]:
        msg_rows = self._conn.execute(
            """
            SELECT event_seq, text_content
            FROM batch_messages
            WHERE batch_id = ?
            ORDER BY received_at_epoch ASC, event_seq ASC
            """,
            (batch_id,),
        ).fetchall()
        event_seqs = tuple(int(r["event_seq"]) for r in msg_rows)
        merged_text = "\n".join(str(r["text_content"]) for r in msg_rows if str(r["text_content"]).strip())
        return event_seqs, merged_text

    def _enqueue_debounce_locked(
        self,
        *,
        account_id: str,
        reader_id: str,
        seq: int,
        text_content: str,
        now_epoch: float,
    ) -> int:
        active_row = self._conn.execute(
            """
            SELECT * FROM merge_batches
            WHERE account_id = ? AND reader_id = ? AND status IN ('open', 'processing') AND preempt_locked = 0
            """,
            (account_id, reader_id),
        ).fetchone()
        if active_row is not None and active_row["status"] == "processing":
            self._conn.execute(
                "UPDATE merge_batches SET preempt_locked = 1 WHERE batch_id = ?",
                (int(active_row["batch_id"]),),
            )
            active_row = None

        if active_row is None:
            quiet_deadline = now_epoch + self.quiet_seconds
            max_deadline = now_epoch + self.max_wait_seconds
            flush_at = min(quiet_deadline, max_deadline)
            batch_cursor = self._conn.execute(
                """
                INSERT INTO merge_batches(
                    account_id, reader_id, status,
                    first_received_at_epoch, last_received_at_epoch,
                    quiet_deadline_epoch, max_deadline_epoch, flush_at_epoch,
                    message_count, route_stage, generation, preempt_count, preempt_locked
                ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, 1, 'pending', 1, 0, 0)
                """,
                (
                    account_id,
                    reader_id,
                    now_epoch,
                    now_epoch,
                    quiet_deadline,
                    max_deadline,
                    flush_at,
                ),
            )
            batch_id = int(batch_cursor.lastrowid)
        else:
            batch_id = int(active_row["batch_id"])
            quiet_deadline = now_epoch + self.quiet_seconds
            max_deadline = float(active_row["max_deadline_epoch"])
            flush_at = min(quiet_deadline, max_deadline)
            self._conn.execute(
                """
                UPDATE merge_batches
                SET last_received_at_epoch = ?,
                    quiet_deadline_epoch = ?,
                    flush_at_epoch = ?,
                    message_count = message_count + 1
                WHERE batch_id = ?
                """,
                (now_epoch, quiet_deadline, flush_at, batch_id),
            )
        self._append_batch_message_locked(
            batch_id=batch_id, seq=seq, now_epoch=now_epoch, text_content=text_content
        )
        return batch_id

    def _enqueue_immediate_locked(
        self,
        *,
        account_id: str,
        reader_id: str,
        seq: int,
        text_content: str,
        now_epoch: float,
    ) -> tuple[int, int | None, int | None]:
        """Enqueue into an immediate-mode generation or queue behind a locked batch."""
        locked_row = self._conn.execute(
            """
            SELECT batch_id FROM merge_batches
            WHERE account_id = ? AND reader_id = ? AND status = 'processing' AND preempt_locked = 1
            ORDER BY batch_id DESC
            LIMIT 1
            """,
            (account_id, reader_id),
        ).fetchone()
        if locked_row is not None:
            batch_id = self._append_or_create_queued_locked(
                account_id=account_id,
                reader_id=reader_id,
                seq=seq,
                text_content=text_content,
                now_epoch=now_epoch,
            )
            return batch_id, None, int(locked_row["batch_id"])

        active_row = self._conn.execute(
            """
            SELECT * FROM merge_batches
            WHERE account_id = ? AND reader_id = ? AND status IN ('open', 'processing') AND preempt_locked = 0
            """,
            (account_id, reader_id),
        ).fetchone()
        if active_row is None:
            max_deadline = now_epoch + self.max_wait_seconds
            batch_cursor = self._conn.execute(
                """
                INSERT INTO merge_batches(
                    account_id, reader_id, status,
                    first_received_at_epoch, last_received_at_epoch,
                    quiet_deadline_epoch, max_deadline_epoch, flush_at_epoch,
                    message_count, merged_text, route_stage, generation, preempt_count, preempt_locked
                ) VALUES (?, ?, 'processing', ?, ?, ?, ?, ?, 1, ?, 'pending', 1, 0, 0)
                """,
                (
                    account_id,
                    reader_id,
                    now_epoch,
                    now_epoch,
                    now_epoch,
                    max_deadline,
                    now_epoch,
                    text_content,
                ),
            )
            batch_id = int(batch_cursor.lastrowid)
            self._append_batch_message_locked(
                batch_id=batch_id, seq=seq, now_epoch=now_epoch, text_content=text_content
            )
            return batch_id, None, None

        active_id = int(active_row["batch_id"])
        can_preempt = (
            int(active_row["preempt_count"]) < self.max_preempts
            and now_epoch < float(active_row["max_deadline_epoch"])
        )
        if can_preempt:
            self._append_batch_message_locked(
                batch_id=active_id, seq=seq, now_epoch=now_epoch, text_content=text_content
            )
            _, merged_text = self._ordered_batch_texts_locked(active_id)
            self._conn.execute(
                """
                UPDATE merge_batches
                SET status = 'processing',
                    last_received_at_epoch = ?,
                    quiet_deadline_epoch = ?,
                    flush_at_epoch = ?,
                    message_count = message_count + 1,
                    merged_text = ?,
                    generation = generation + 1,
                    preempt_count = preempt_count + 1
                WHERE batch_id = ?
                """,
                (now_epoch, now_epoch, now_epoch, merged_text, active_id),
            )
            return active_id, active_id, None

        # Preempt limit or time cap reached: let the in-flight batch finish undisturbed.
        self._conn.execute(
            "UPDATE merge_batches SET preempt_locked = 1 WHERE batch_id = ?",
            (active_id,),
        )
        queued_id = self._append_or_create_queued_locked(
            account_id=account_id,
            reader_id=reader_id,
            seq=seq,
            text_content=text_content,
            now_epoch=now_epoch,
        )
        return queued_id, None, active_id

    def _append_or_create_queued_locked(
        self,
        *,
        account_id: str,
        reader_id: str,
        seq: int,
        text_content: str,
        now_epoch: float,
    ) -> int:
        queued_row = self._conn.execute(
            """
            SELECT batch_id FROM merge_batches
            WHERE account_id = ? AND reader_id = ? AND status = 'queued'
            """,
            (account_id, reader_id),
        ).fetchone()
        if queued_row is None:
            max_deadline = now_epoch + self.max_wait_seconds
            cursor = self._conn.execute(
                """
                INSERT INTO merge_batches(
                    account_id, reader_id, status,
                    first_received_at_epoch, last_received_at_epoch,
                    quiet_deadline_epoch, max_deadline_epoch, flush_at_epoch,
                    message_count, merged_text, route_stage, generation, preempt_count, preempt_locked
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, 1, ?, 'pending', 1, 0, 0)
                """,
                (
                    account_id,
                    reader_id,
                    now_epoch,
                    now_epoch,
                    now_epoch,
                    max_deadline,
                    now_epoch,
                    text_content,
                ),
            )
            batch_id = int(cursor.lastrowid)
            self._append_batch_message_locked(
                batch_id=batch_id, seq=seq, now_epoch=now_epoch, text_content=text_content
            )
            return batch_id

        batch_id = int(queued_row["batch_id"])
        self._append_batch_message_locked(
            batch_id=batch_id, seq=seq, now_epoch=now_epoch, text_content=text_content
        )
        _, merged_text = self._ordered_batch_texts_locked(batch_id)
        self._conn.execute(
            """
            UPDATE merge_batches
            SET last_received_at_epoch = ?,
                message_count = message_count + 1,
                merged_text = ?
            WHERE batch_id = ?
            """,
            (now_epoch, merged_text, batch_id),
        )
        return batch_id

    def _compute_batch_route_locked(
        self,
        *,
        batch_id: int,
        account_id: str,
        reader_id: str,
        now_epoch: float,
    ) -> tuple[tuple[int, ...], str, str, int | None, str]:
        event_seqs, merged_text = self._ordered_batch_texts_locked(batch_id)
        open_case_row = self._conn.execute(
            """
            SELECT * FROM staff_cases
            WHERE account_id = ? AND reader_id = ? AND case_kind = 'staff_todo' AND status = 'open'
            ORDER BY updated_at_epoch DESC, case_id DESC
            LIMIT 1
            """,
            (account_id, reader_id),
        ).fetchone()
        route_stage = "ready_for_triage"
        matched_case_id: int | None = None
        same_case_reason = "no_open_case"
        if open_case_row is not None:
            case_dict = self._row_to_case(open_case_row).to_dict()
            is_same, same_case_reason = self._same_case_matcher(merged_text, case_dict, now_epoch)
            if is_same:
                matched_case_id = int(open_case_row["case_id"])
                route_stage = "staff_case_appended"
        return event_seqs, merged_text, route_stage, matched_case_id, same_case_reason

    def _apply_flush_commit_locked(
        self,
        *,
        batch_id: int,
        event_seqs: tuple[int, ...],
        merged_text: str,
        route_stage: str,
        matched_case_id: int | None,
        now_epoch: float,
        expected_status: str,
        expected_generation: int | None = None,
    ) -> bool:
        now_utc = _utc_iso(now_epoch)
        where_clause = "WHERE batch_id = ? AND status = ?"
        params: list[Any] = [merged_text, now_epoch, route_stage, matched_case_id, batch_id, expected_status]
        if expected_generation is not None:
            where_clause += " AND generation = ?"
            params.append(expected_generation)

        cursor = self._conn.execute(
            f"""
            UPDATE merge_batches
            SET status = 'flushed',
                merged_text = ?,
                flushed_at_epoch = ?,
                route_stage = ?,
                staff_case_id = ?
            {where_clause}
            """,
            params,
        )
        if cursor.rowcount == 0:
            return False

        if matched_case_id is not None:
            self._conn.execute(
                """
                UPDATE staff_cases
                SET latest_message = ?, updated_at_epoch = ?, updated_at_utc = ?
                WHERE case_id = ?
                """,
                (merged_text, now_epoch, now_utc, matched_case_id),
            )
            self._conn.execute(
                """
                INSERT INTO staff_case_messages(
                    case_id, batch_id, event_seq, sender_role, content, created_at_epoch, created_at_utc
                ) VALUES (?, ?, ?, 'reader', ?, ?, ?)
                """,
                (
                    matched_case_id,
                    batch_id,
                    event_seqs[-1] if event_seqs else None,
                    merged_text,
                    now_epoch,
                    now_utc,
                ),
            )
            self._conn.execute(
                "UPDATE inbound_events SET staff_case_id = ? WHERE batch_id = ?",
                (matched_case_id, batch_id),
            )
        return True

    def _flush_batch_locked(self, batch_id: int, *, now_epoch: float) -> FlushOutcome | None:
        batch_row = self._conn.execute(
            "SELECT * FROM merge_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch_row is None or batch_row["status"] != "open":
            return None

        event_seqs, merged_text, route_stage, matched_case_id, same_case_reason = (
            self._compute_batch_route_locked(
                batch_id=batch_id,
                account_id=str(batch_row["account_id"]),
                reader_id=str(batch_row["reader_id"]),
                now_epoch=now_epoch,
            )
        )
        with self._conn:
            committed = self._apply_flush_commit_locked(
                batch_id=batch_id,
                event_seqs=event_seqs,
                merged_text=merged_text,
                route_stage=route_stage,
                matched_case_id=matched_case_id,
                now_epoch=now_epoch,
                expected_status="open",
            )
        if not committed:
            return None
        updated_batch = self.get_batch(batch_id)
        assert updated_batch is not None
        return FlushOutcome(
            batch=updated_batch,
            event_seqs=event_seqs,
            route_stage=route_stage,
            staff_case_id=matched_case_id,
            same_case_reason=same_case_reason,
        )

    def prepare_immediate_generation(
        self, batch_id: int, expected_generation: int, *, now_epoch: float | None = None
    ) -> FlushOutcome | None:
        """Compute current merged text and staff-case route without closing the processing batch."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            batch_row = self._conn.execute(
                "SELECT * FROM merge_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if (
                batch_row is None
                or batch_row["status"] != "processing"
                or int(batch_row["generation"]) != expected_generation
            ):
                return None
            event_seqs, merged_text, route_stage, matched_case_id, same_case_reason = (
                self._compute_batch_route_locked(
                    batch_id=batch_id,
                    account_id=str(batch_row["account_id"]),
                    reader_id=str(batch_row["reader_id"]),
                    now_epoch=now_val,
                )
            )
            snapshot = self._row_to_batch(batch_row)
            return FlushOutcome(
                batch=snapshot,
                event_seqs=event_seqs,
                route_stage=route_stage,
                staff_case_id=matched_case_id,
                same_case_reason=same_case_reason,
            )

    def commit_batch_generation(
        self,
        batch_id: int,
        expected_generation: int,
        *,
        route_stage: str,
        staff_case_id: int | None = None,
        same_case_reason: str = "",
        now_epoch: float | None = None,
    ) -> tuple[FlushOutcome | None, BatchSnapshot | None]:
        """Atomically commit a processing batch iff its generation was not preempted."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            batch_row = self._conn.execute(
                "SELECT * FROM merge_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if (
                batch_row is None
                or batch_row["status"] != "processing"
                or int(batch_row["generation"]) != expected_generation
            ):
                return None, None

            event_seqs, merged_text = self._ordered_batch_texts_locked(batch_id)
            account_id = str(batch_row["account_id"])
            reader_id = str(batch_row["reader_id"])
            promoted_snapshot: BatchSnapshot | None = None

            with self._conn:
                committed = self._apply_flush_commit_locked(
                    batch_id=batch_id,
                    event_seqs=event_seqs,
                    merged_text=merged_text,
                    route_stage=route_stage,
                    matched_case_id=staff_case_id,
                    now_epoch=now_val,
                    expected_status="processing",
                    expected_generation=expected_generation,
                )
                if not committed:
                    return None, None

                queued_row = self._conn.execute(
                    """
                    SELECT batch_id FROM merge_batches
                    WHERE account_id = ? AND reader_id = ? AND status = 'queued'
                    ORDER BY batch_id ASC
                    LIMIT 1
                    """,
                    (account_id, reader_id),
                ).fetchone()
                if queued_row is not None:
                    queued_id = int(queued_row["batch_id"])
                    self._conn.execute(
                        """
                        UPDATE merge_batches
                        SET status = 'processing',
                            generation = 1,
                            preempt_count = 0,
                            preempt_locked = 0,
                            max_deadline_epoch = ?,
                            flush_at_epoch = ?
                        WHERE batch_id = ? AND status = 'queued'
                        """,
                        (now_val + self.max_wait_seconds, now_val, queued_id),
                    )
                    promoted_snapshot = self.get_batch(queued_id)

            updated_batch = self.get_batch(batch_id)
            assert updated_batch is not None
            return (
                FlushOutcome(
                    batch=updated_batch,
                    event_seqs=event_seqs,
                    route_stage=route_stage,
                    staff_case_id=staff_case_id,
                    same_case_reason=same_case_reason,
                ),
                promoted_snapshot,
            )

    def recover_immediate_batches(self, *, now_epoch: float | None = None) -> list[BatchSnapshot]:
        """Return unfinished processing batches and promote any orphaned queued batches."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            with self._conn:
                orphan_rows = self._conn.execute(
                    """
                    SELECT q.batch_id
                    FROM merge_batches AS q
                    WHERE q.status = 'queued'
                      AND NOT EXISTS (
                          SELECT 1 FROM merge_batches AS active
                          WHERE active.account_id = q.account_id
                            AND active.reader_id = q.reader_id
                            AND active.status = 'processing'
                      )
                    """
                ).fetchall()
                for row in orphan_rows:
                    self._conn.execute(
                        """
                        UPDATE merge_batches
                        SET status = 'processing',
                            generation = 1,
                            preempt_locked = 0,
                            max_deadline_epoch = ?
                        WHERE batch_id = ? AND status = 'queued'
                        """,
                        (now_val + self.max_wait_seconds, int(row["batch_id"])),
                    )
            rows = self._conn.execute(
                "SELECT * FROM merge_batches WHERE status = 'processing' ORDER BY batch_id ASC"
            ).fetchall()
            return [self._row_to_batch(r) for r in rows]

    def flush_batch(self, batch_id: int, *, now_epoch: float | None = None) -> FlushOutcome | None:
        """Flush a single open batch immediately regardless of deadline."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            return self._flush_batch_locked(batch_id, now_epoch=now_val)

    def flush_due_batches(self, *, now_epoch: float | None = None) -> list[FlushOutcome]:
        """Flush all open batches whose flush_at_epoch <= now, returning their outcomes."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            due_rows = self._conn.execute(
                """
                SELECT batch_id FROM merge_batches
                WHERE status = 'open' AND flush_at_epoch <= ?
                ORDER BY flush_at_epoch ASC, batch_id ASC
                """,
                (now_val,),
            ).fetchall()
            outcomes: list[FlushOutcome] = []
            for row in due_rows:
                outcome = self._flush_batch_locked(int(row["batch_id"]), now_epoch=now_val)
                if outcome is not None:
                    outcomes.append(outcome)
            return outcomes

    def next_open_deadline(self) -> float | None:
        """Return the earliest flush_at_epoch among currently open batches, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(flush_at_epoch) AS next_flush FROM merge_batches WHERE status = 'open'"
            ).fetchone()
            if row is None or row["next_flush"] is None:
                return None
            return float(row["next_flush"])

    def create_staff_case(
        self,
        *,
        account_id: str,
        reader_id: str,
        summary: str,
        latest_message: str = "",
        source_batch_id: int | None = None,
        source_event_seq: int | None = None,
        case_kind: str = "staff_todo",
        now_epoch: float | None = None,
    ) -> StaffCaseSnapshot:
        """Create a staff case (e.g., Fallback handoff to-do) and mark it open."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            body = latest_message or summary
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO staff_cases(
                        account_id, reader_id, source_batch_id, source_event_seq,
                        case_kind, status, summary, latest_message,
                        created_at_epoch, created_at_utc, updated_at_epoch, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id or "default",
                        reader_id,
                        source_batch_id,
                        source_event_seq,
                        case_kind,
                        summary,
                        body,
                        now_val,
                        now_utc,
                        now_val,
                        now_utc,
                    ),
                )
                case_id = int(cursor.lastrowid)
                self._conn.execute(
                    """
                    INSERT INTO staff_case_messages(
                        case_id, batch_id, event_seq, sender_role, content, created_at_epoch, created_at_utc
                    ) VALUES (?, ?, ?, 'reader', ?, ?, ?)
                    """,
                    (case_id, source_batch_id, source_event_seq, body, now_val, now_utc),
                )
                if source_batch_id is not None:
                    self._conn.execute(
                        "UPDATE merge_batches SET staff_case_id = ? WHERE batch_id = ?",
                        (case_id, source_batch_id),
                    )
            row = self._conn.execute("SELECT * FROM staff_cases WHERE case_id = ?", (case_id,)).fetchone()
            assert row is not None
            return self._row_to_case(row)

    def close_staff_case(
        self,
        case_id: int,
        *,
        resolution_note: str = "",
        closed_by: str = "",
        now_epoch: float | None = None,
    ) -> StaffCaseSnapshot | None:
        """Mark a staff case resolved on the Dashboard so later questions take normal flow."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            clean_note = resolution_note.strip()
            clean_operator = closed_by.strip()
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE staff_cases
                    SET status = 'closed',
                        closed_at_utc = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?,
                        resolution_note = ?,
                        closed_by = ?
                    WHERE case_id = ? AND status = 'open'
                    """,
                    (now_utc, now_val, now_utc, clean_note, clean_operator, case_id),
                )
                if cursor.rowcount > 0 and clean_note:
                    self._conn.execute(
                        """
                        INSERT INTO staff_case_messages(
                            case_id, sender_role, content, operator_username, created_at_epoch, created_at_utc
                        ) VALUES (?, 'staff', ?, ?, ?, ?)
                        """,
                        (case_id, clean_note, clean_operator, now_val, now_utc),
                    )
            row = self._conn.execute("SELECT * FROM staff_cases WHERE case_id = ?", (case_id,)).fetchone()
            return self._row_to_case(row) if row is not None else None

    def record_outbound_reply(
        self,
        *,
        account_id: str,
        reader_id: str,
        trigger_kind: str,
        trigger_ref_id: int | None,
        content: str,
        delivery_mode: str,
        status: str,
        errcode: int | None = None,
        errmsg: str = "",
        duration_ms: int | None = None,
        operator_username: str = "",
        now_epoch: float | None = None,
    ) -> int:
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO outbound_replies(
                        account_id, reader_id, trigger_kind, trigger_ref_id,
                        content, delivery_mode, status, errcode, errmsg, duration_ms,
                        operator_username, created_at_epoch, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id or "default",
                        reader_id,
                        trigger_kind,
                        trigger_ref_id,
                        content,
                        delivery_mode,
                        status,
                        errcode,
                        errmsg,
                        duration_ms,
                        operator_username.strip(),
                        now_val,
                        now_utc,
                    ),
                )
                return int(cursor.lastrowid)

    def get_batch(self, batch_id: int) -> BatchSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM merge_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return self._row_to_batch(row) if row is not None else None

    def list_events(self, *, limit: int = 20, category: str | None = None) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        where_sql = "WHERE filter_category = ?" if category else ""
        params: tuple[Any, ...] = (category, bounded) if category else (bounded,)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT seq, received_at_utc, received_at_epoch, transport, account_id, reader_id,
                       msg_type, event, msg_id, content, filter_category, filter_reason,
                       batch_id, staff_case_id, feedback_id
                FROM inbound_events
                {where_sql}
                ORDER BY seq DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_librarian_inbox(self, *, before: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return retained reader messages for librarian inbox pages without exposing reader_id."""
        bounded = max(1, min(limit, 200))
        with self._lock:
            now = float(self._clock())
            clauses = [
                "reader_id <> ''",
                "msg_type <> 'event'",
                "event = ''",
                "received_at_epoch >= ?",
                "(expires_at_epoch IS NULL OR expires_at_epoch > ?)",
            ]
            params: list[Any] = [now - DEFAULT_RETENTION_SECONDS, now]
            if before is not None:
                clauses.append("seq < ?")
                params.append(int(before))
            params.append(bounded)
            rows = self._conn.execute(
                f"""
                SELECT seq, received_at_utc, msg_type, content, filter_category, account_id, reader_id
                FROM inbound_events
                WHERE {' AND '.join(clauses)}
                ORDER BY seq DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            if not rows:
                return []

            seqs = [int(row["seq"]) for row in rows]
            placeholders = ",".join("?" for _ in seqs)
            reply_rows = self._conn.execute(
                f"""
                SELECT trigger_ref_id, content, created_at_utc, operator_username, status
                FROM outbound_replies
                WHERE trigger_kind = 'staff_dashboard_reply'
                  AND trigger_ref_id IN ({placeholders})
                ORDER BY reply_id ASC
                """,
                seqs,
            ).fetchall()
            replies_by_seq: dict[int, list[dict[str, str]]] = {}
            for rrow in reply_rows:
                ref_id = int(rrow["trigger_ref_id"])
                replies_by_seq.setdefault(ref_id, []).append(
                    {
                        "content": str(rrow["content"]),
                        "created_at_utc": str(rrow["created_at_utc"]),
                        "operator_username": str(rrow["operator_username"] or ""),
                        "status": str(rrow["status"]),
                    }
                )

            quota_cache: dict[tuple[str, str], dict[str, Any]] = {}
            results: list[dict[str, Any]] = []
            for row in rows:
                seq = int(row["seq"])
                key = (str(row["account_id"]), str(row["reader_id"]))
                if key not in quota_cache:
                    quota_cache[key] = self._reader_reply_quota_locked(
                        key[0], key[1], now_epoch=now
                    )
                results.append(
                    {
                        "seq": seq,
                        "received_at_utc": str(row["received_at_utc"]),
                        "msg_type": str(row["msg_type"]),
                        "content": str(row["content"]),
                        "filter_category": str(row["filter_category"]),
                        "reply_quota": dict(quota_cache[key]),
                        "staff_replies": list(replies_by_seq.get(seq, [])),
                    }
                )
            return results

    def list_batches(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        where_sql = "WHERE status = ?" if status else ""
        params: tuple[Any, ...] = (status, bounded) if status else (bounded,)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM merge_batches {where_sql} ORDER BY batch_id DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._row_to_batch(row).to_dict() for row in rows]

    def list_feedbacks(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT feedback_id, account_id, reader_id, event_seq, created_at_utc, content, status, staff_note
                FROM feedback_records
                ORDER BY feedback_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_staff_cases(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        where_sql = "WHERE status = ?" if status else ""
        params: tuple[Any, ...] = (status, bounded) if status else (bounded,)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM staff_cases
                {where_sql}
                ORDER BY updated_at_epoch DESC, case_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            cases = [self._row_to_case(row).to_dict() for row in rows]
            for item in cases:
                msgs = self._conn.execute(
                    """
                    SELECT item_id, batch_id, event_seq, sender_role, content, operator_username, created_at_utc
                    FROM staff_case_messages
                    WHERE case_id = ?
                    ORDER BY item_id ASC
                    """,
                    (item["case_id"],),
                ).fetchall()
                item["messages"] = [dict(m) for m in msgs]
            return cases

    def get_active_clarification(
        self, account_id: str, reader_id: str, *, now_epoch: float | None = None
    ) -> ClarificationSnapshot | None:
        """Return unexpired pending clarification for `(account_id, reader_id)`, expiring stale ones."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE clarification_states
                    SET status = 'expired'
                    WHERE account_id = ? AND reader_id = ? AND status = 'awaiting_reply' AND expires_at_epoch <= ?
                    """,
                    (account_id or "default", reader_id, now_val),
                )
            row = self._conn.execute(
                """
                SELECT * FROM clarification_states
                WHERE account_id = ? AND reader_id = ? AND status = 'awaiting_reply'
                LIMIT 1
                """,
                (account_id or "default", reader_id),
            ).fetchone()
            if row is None:
                return None
            return ClarificationSnapshot(
                clarify_id=int(row["clarify_id"]),
                account_id=str(row["account_id"]),
                reader_id=str(row["reader_id"]),
                status=str(row["status"]),
                original_batch_id=int(row["original_batch_id"]) if row["original_batch_id"] is not None else None,
                original_question=str(row["original_question"]),
                clarify_prompt=str(row["clarify_prompt"]),
                attempt_count=int(row["attempt_count"]),
                asked_at_epoch=float(row["asked_at_epoch"]),
                asked_at_utc=str(row["asked_at_utc"]),
                expires_at_epoch=float(row["expires_at_epoch"]),
                consumed_at_utc=row["consumed_at_utc"],
            )

    def set_active_clarification(
        self,
        *,
        account_id: str,
        reader_id: str,
        original_batch_id: int | None,
        original_question: str,
        clarify_prompt: str,
        ttl_seconds: float = DEFAULT_CLARIFICATION_TTL_SECONDS,
        now_epoch: float | None = None,
    ) -> int:
        """Persist a new single clarification state, expiring any prior pending row."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            acct = account_id or "default"
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE clarification_states
                    SET status = 'expired'
                    WHERE account_id = ? AND reader_id = ? AND status = 'awaiting_reply'
                    """,
                    (acct, reader_id),
                )
                cursor = self._conn.execute(
                    """
                    INSERT INTO clarification_states(
                        account_id, reader_id, status, original_batch_id,
                        original_question, clarify_prompt, attempt_count,
                        asked_at_epoch, asked_at_utc, expires_at_epoch
                    ) VALUES (?, ?, 'awaiting_reply', ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        acct,
                        reader_id,
                        original_batch_id,
                        original_question,
                        clarify_prompt,
                        now_val,
                        now_utc,
                        now_val + max(60.0, ttl_seconds),
                    ),
                )
                return int(cursor.lastrowid)

    def consume_active_clarification(
        self, account_id: str, reader_id: str, *, now_epoch: float | None = None
    ) -> None:
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE clarification_states
                    SET status = 'consumed', consumed_at_utc = ?
                    WHERE account_id = ? AND reader_id = ? AND status = 'awaiting_reply'
                    """,
                    (now_utc, account_id or "default", reader_id),
                )

    def record_triage_run(
        self,
        *,
        batch_id: int,
        account_id: str,
        reader_id: str,
        generation: int,
        clarified_already: bool,
        stage_path: str,
        decision_route: str = "",
        decision_preset_id: str = "",
        decision_confidence: float | None = None,
        decision_duration_ms: int | None = None,
        preset_id: str = "",
        source_url: str = "",
        section_title: str = "",
        reply_preview: str = "",
        staff_case_id: int | None = None,
        now_epoch: float | None = None,
    ) -> int:
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO triage_runs(
                        batch_id, account_id, reader_id, generation, clarified_already,
                        stage_path, decision_route, decision_preset_id, decision_confidence,
                        decision_duration_ms, preset_id, source_url, section_title,
                        reply_preview, staff_case_id, created_at_epoch, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        account_id or "default",
                        reader_id,
                        generation,
                        1 if clarified_already else 0,
                        stage_path,
                        decision_route,
                        decision_preset_id,
                        decision_confidence,
                        decision_duration_ms,
                        preset_id,
                        source_url,
                        section_title,
                        reply_preview[:160],
                        staff_case_id,
                        now_val,
                        now_utc,
                    ),
                )
                return int(cursor.lastrowid)

    def list_triage_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT run_id, batch_id, account_id, reader_id, generation, clarified_already,
                       stage_path, decision_route, decision_preset_id, decision_confidence,
                       decision_duration_ms, preset_id, source_url, section_title,
                       reply_preview, staff_case_id, created_at_utc
                FROM triage_runs
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_outbound_replies(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT reply_id, account_id, reader_id, trigger_kind, trigger_ref_id,
                       content, delivery_mode, status, errcode, errmsg, duration_ms,
                       operator_username, created_at_utc
                FROM outbound_replies
                ORDER BY reply_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            return [dict(row) for row in rows]

    def overview_metrics(self) -> dict[str, Any]:
        """Return aggregate operational counts without exposing raw messages or OpenIDs."""
        with self._lock:
            cat_rows = self._conn.execute(
                "SELECT filter_category, COUNT(*) AS cnt FROM inbound_events GROUP BY filter_category"
            ).fetchall()
            categories = {str(r["filter_category"]): int(r["cnt"]) for r in cat_rows}
            open_batches = int(
                self._conn.execute("SELECT COUNT(*) FROM merge_batches WHERE status = 'open'").fetchone()[0]
            )
            processing_batches = int(
                self._conn.execute("SELECT COUNT(*) FROM merge_batches WHERE status = 'processing'").fetchone()[0]
            )
            queued_batches = int(
                self._conn.execute("SELECT COUNT(*) FROM merge_batches WHERE status = 'queued'").fetchone()[0]
            )
            flushed_batches = int(
                self._conn.execute("SELECT COUNT(*) FROM merge_batches WHERE status = 'flushed'").fetchone()[0]
            )
            preempted_total = int(
                self._conn.execute("SELECT COALESCE(SUM(preempt_count), 0) FROM merge_batches").fetchone()[0]
            )
            multi_message_batches = int(
                self._conn.execute("SELECT COUNT(*) FROM merge_batches WHERE message_count > 1").fetchone()[0]
            )
            open_cases = int(
                self._conn.execute("SELECT COUNT(*) FROM staff_cases WHERE status = 'open'").fetchone()[0]
            )
            feedback_total = int(
                self._conn.execute("SELECT COUNT(*) FROM feedback_records").fetchone()[0]
            )
            reply_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM outbound_replies GROUP BY status"
            ).fetchall()
            reply_statuses = {str(r["status"]): int(r["cnt"]) for r in reply_rows}
            triage_rows = self._conn.execute(
                "SELECT stage_path, COUNT(*) AS cnt FROM triage_runs GROUP BY stage_path"
            ).fetchall()
            triage_stages = {str(r["stage_path"]): int(r["cnt"]) for r in triage_rows}
            active_clarifications = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM clarification_states WHERE status = 'awaiting_reply'"
                ).fetchone()[0]
            )
            dynamic_allowlist_count = int(
                self._conn.execute("SELECT COUNT(*) FROM auto_reply_allowlist").fetchone()[0]
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "merge_enabled": self.merge_enabled,
                "max_preempts": self.max_preempts,
                "quiet_seconds": self.quiet_seconds,
                "max_wait_seconds": self.max_wait_seconds,
                "inbound_categories": categories,
                "open_batches": open_batches,
                "processing_batches": processing_batches,
                "queued_batches": queued_batches,
                "flushed_batches": flushed_batches,
                "preempted_total": preempted_total,
                "multi_message_batches": multi_message_batches,
                "open_staff_cases": open_cases,
                "active_clarifications": active_clarifications,
                "dynamic_allowlist_count": dynamic_allowlist_count,
                "feedback_count": feedback_total,
                "triage_stages": triage_stages,
                "outbound_replies": reply_statuses,
            }

    def purge_expired_events(self, *, now_epoch: float | None = None) -> int:
        """Delete expired inbound_events not referenced by active merge batches while preserving FK integrity."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            rows = self._conn.execute(
                """
                SELECT ie.seq
                FROM inbound_events AS ie
                WHERE ie.expires_at_epoch IS NOT NULL
                  AND ie.expires_at_epoch <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM batch_messages AS bm
                      JOIN merge_batches AS mb ON mb.batch_id = bm.batch_id
                      WHERE bm.event_seq = ie.seq
                        AND mb.status IN ('open', 'processing', 'queued')
                  )
                """,
                (now_val,),
            ).fetchall()
            expired_seqs = [int(row["seq"]) for row in rows]
            if not expired_seqs:
                return 0

            deleted_total = 0
            with self._conn:
                for start in range(0, len(expired_seqs), _PURGE_CHUNK_SIZE):
                    chunk = expired_seqs[start : start + _PURGE_CHUNK_SIZE]
                    placeholders = ",".join("?" for _ in chunk)
                    self._conn.execute(
                        f"DELETE FROM batch_messages WHERE event_seq IN ({placeholders})",
                        chunk,
                    )
                    self._conn.execute(
                        f"UPDATE staff_case_messages SET event_seq = NULL WHERE event_seq IN ({placeholders})",
                        chunk,
                    )
                    cursor = self._conn.execute(
                        f"DELETE FROM inbound_events WHERE seq IN ({placeholders})",
                        chunk,
                    )
                    deleted_total += int(cursor.rowcount)
            return deleted_total

    def dashboard_admin_analytics(self, *, now_epoch: float | None = None) -> dict[str, Any]:
        """Return UTC+8 operational analytics for the admin dashboard without exposing reader content or OpenIDs."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_dt = _epoch_to_utc8(now_val)
            today_start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_epoch = today_start_dt.timestamp()
            tomorrow_start_epoch = (today_start_dt + timedelta(days=1)).timestamp()

            day_dts = [today_start_dt - timedelta(days=offset) for offset in range(6, -1, -1)]
            day0_dt = day_dts[0]
            day0_epoch = day0_dt.timestamp()
            prior_7d_start_epoch = (day0_dt - timedelta(days=7)).timestamp()

            day_counts: dict[str, int] = {dt.strftime("%Y-%m-%d"): 0 for dt in day_dts}
            hour_counts: dict[int, int] = {}
            filter_counts: dict[str, int] = {
                "question": 0,
                "feedback": 0,
                "invalid": 0,
                "unsupported": 0,
                "emergency": 0,
                "gray_enroll": 0,
            }
            total_non_gray = 0
            menu_events_7d = 0
            menu_events_today = 0

            inbound_7d_rows = self._conn.execute(
                """
                SELECT received_at_epoch, filter_category, msg_type, event
                FROM inbound_events
                WHERE received_at_epoch >= ? AND received_at_epoch < ?
                """,
                (day0_epoch, tomorrow_start_epoch),
            ).fetchall()
            for row in inbound_7d_rows:
                rec_epoch = float(row["received_at_epoch"])
                msg_type = str(row["msg_type"] or "").strip().lower()
                event_name = str(row["event"] or "").strip()
                if msg_type == "event" or event_name:
                    menu_events_7d += 1
                    if today_start_epoch <= rec_epoch < tomorrow_start_epoch:
                        menu_events_today += 1
                    continue
                rec_dt = _epoch_to_utc8(rec_epoch)
                day_key = rec_dt.strftime("%Y-%m-%d")
                if day_key in day_counts:
                    day_counts[day_key] += 1
                if today_start_epoch <= rec_epoch < tomorrow_start_epoch:
                    hour_counts[rec_dt.hour] = hour_counts.get(rec_dt.hour, 0) + 1
                cat = str(row["filter_category"])
                if cat in filter_counts:
                    filter_counts[cat] += 1
                if cat != "gray_enroll":
                    total_non_gray += 1

            daily_inbound = [
                {
                    "date": dt.strftime("%Y-%m-%d"),
                    "label": f"{dt.day:02d}{_WEEKDAY_ZH[dt.weekday()]}",
                    "count": day_counts[dt.strftime("%Y-%m-%d")],
                }
                for dt in day_dts
            ]
            inbound_7d_total = sum(item["count"] for item in daily_inbound)
            inbound_7d_avg = round(inbound_7d_total / 7.0, 1)
            inbound_today_total = daily_inbound[-1]["count"]
            inbound_all_total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM inbound_events WHERE msg_type <> 'event' AND event = ''"
                ).fetchone()[0]
            )

            prior_7d_total = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM inbound_events
                    WHERE msg_type <> 'event' AND event = ''
                      AND received_at_epoch >= ? AND received_at_epoch < ?
                    """,
                    (prior_7d_start_epoch, day0_epoch),
                ).fetchone()[0]
            )
            inbound_delta_pct = (
                round((inbound_7d_total - prior_7d_total) / prior_7d_total * 100.0, 1)
                if prior_7d_total > 0
                else None
            )

            active_hours = [h for h, cnt in hour_counts.items() if cnt > 0]
            cur_hour = now_dt.hour
            start_h = min(8, min(active_hours)) if active_hours else 8
            end_h = max(16, cur_hour, max(active_hours)) if active_hours else max(16, cur_hour)
            start_h = max(0, min(23, start_h))
            end_h = max(start_h, min(23, end_h))
            hourly_inbound_today = [
                {"hour": h, "label": f"{h:02d}", "count": hour_counts.get(h, 0)}
                for h in range(start_h, end_h + 1)
            ]
            hourly_range_label = f"{start_h:02d}–{end_h:02d}h"

            replies_today = {"sent": 0, "shadow": 0, "failed": 0, "total": 0}
            for row in self._conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM outbound_replies
                WHERE created_at_epoch >= ? AND created_at_epoch < ?
                GROUP BY status
                """,
                (today_start_epoch, tomorrow_start_epoch),
            ).fetchall():
                cnt = int(row["cnt"])
                status = str(row["status"])
                replies_today["total"] += cnt
                if status in ("sent", "shadow", "failed"):
                    replies_today[status] += cnt

            replies_all = {"sent": 0, "shadow": 0, "failed": 0, "total": 0}
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM outbound_replies GROUP BY status"
            ).fetchall():
                cnt = int(row["cnt"])
                status = str(row["status"])
                replies_all["total"] += cnt
                if status in ("sent", "shadow", "failed"):
                    replies_all[status] += cnt

            filter_7d = {**filter_counts, "total_non_gray": total_non_gray}

            routing_7d: dict[str, Any] = {
                "tier1_preset": 0,
                "tier2_rag": 0,
                "clarify": 0,
                "staff_fallback": 0,
                "other": 0,
            }
            for row in self._conn.execute(
                """
                SELECT stage_path, COUNT(*) AS cnt
                FROM triage_runs
                WHERE created_at_epoch >= ? AND created_at_epoch < ?
                GROUP BY stage_path
                """,
                (day0_epoch, tomorrow_start_epoch),
            ).fetchall():
                stage_path = str(row["stage_path"])
                cnt = int(row["cnt"])
                if stage_path in _ROUTING_TIER1_PRESET:
                    routing_7d["tier1_preset"] += cnt
                elif stage_path in _ROUTING_TIER2_RAG:
                    routing_7d["tier2_rag"] += cnt
                elif stage_path in _ROUTING_CLARIFY:
                    routing_7d["clarify"] += cnt
                elif stage_path in _ROUTING_STAFF_FALLBACK:
                    routing_7d["staff_fallback"] += cnt
                else:
                    routing_7d["other"] += cnt
            routing_total = (
                routing_7d["tier1_preset"]
                + routing_7d["tier2_rag"]
                + routing_7d["clarify"]
                + routing_7d["staff_fallback"]
                + routing_7d["other"]
            )
            routing_7d["total"] = routing_total
            routing_7d["auto_rate_pct"] = (
                round((routing_total - routing_7d["staff_fallback"]) / routing_total * 100.0, 1)
                if routing_total > 0
                else None
            )

            batch_counts = {"open": 0, "processing": 0, "queued": 0, "flushed": 0}
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM merge_batches GROUP BY status"
            ).fetchall():
                status = str(row["status"])
                if status in batch_counts:
                    batch_counts[status] = int(row["cnt"])
            batches = {
                **batch_counts,
                "active_total": (
                    batch_counts["open"] + batch_counts["processing"] + batch_counts["queued"]
                ),
            }

            open_emergency_count = 0
            open_todo_count = 0
            for row in self._conn.execute(
                """
                SELECT case_kind, COUNT(*) AS cnt
                FROM staff_cases
                WHERE status = 'open'
                GROUP BY case_kind
                """
            ).fetchall():
                if str(row["case_kind"]) == "emergency":
                    open_emergency_count += int(row["cnt"])
                else:
                    open_todo_count += int(row["cnt"])
            open_cases = {
                "total": open_emergency_count + open_todo_count,
                "emergency": open_emergency_count,
                "staff_todo": open_todo_count,
            }

            alert_candidates: list[tuple[float, int, dict[str, str]]] = []
            for row in self._conn.execute(
                """
                SELECT reply_id, errcode, created_at_epoch, created_at_utc
                FROM outbound_replies
                WHERE status = 'failed'
                ORDER BY created_at_epoch DESC, reply_id DESC
                LIMIT 5
                """
            ).fetchall():
                ep = float(row["created_at_epoch"])
                errcode = row["errcode"]
                err_suffix = f" ({int(errcode)})" if errcode else ""
                alert_candidates.append(
                    (
                        ep,
                        int(row["reply_id"]),
                        {
                            "time_hm": _epoch_to_utc8(ep).strftime("%H:%M"),
                            "time_iso": str(row["created_at_utc"]),
                            "kind": "reply_failed",
                            "badge": "发信失败",
                            "severity": "urgent",
                            "text": f"微信客服接口请求失败{err_suffix}",
                        },
                    )
                )
            for row in self._conn.execute(
                """
                SELECT case_id, created_at_epoch, created_at_utc
                FROM staff_cases
                WHERE case_kind = 'emergency'
                ORDER BY created_at_epoch DESC, case_id DESC
                LIMIT 5
                """
            ).fetchall():
                ep = float(row["created_at_epoch"])
                alert_candidates.append(
                    (
                        ep,
                        int(row["case_id"]),
                        {
                            "time_hm": _epoch_to_utc8(ep).strftime("%H:%M"),
                            "time_iso": str(row["created_at_utc"]),
                            "kind": "emergency",
                            "badge": "紧急事件",
                            "severity": "urgent",
                            "text": "收到紧急情况登记，已回复联系方式",
                        },
                    )
                )
            for row in self._conn.execute(
                """
                SELECT run_id, created_at_epoch, created_at_utc
                FROM triage_runs
                WHERE staff_case_id IS NOT NULL
                ORDER BY created_at_epoch DESC, run_id DESC
                LIMIT 5
                """
            ).fetchall():
                ep = float(row["created_at_epoch"])
                alert_candidates.append(
                    (
                        ep,
                        int(row["run_id"]),
                        {
                            "time_hm": _epoch_to_utc8(ep).strftime("%H:%M"),
                            "time_iso": str(row["created_at_utc"]),
                            "kind": "staff_fallback",
                            "badge": "馆员兜底",
                            "severity": "muted",
                            "text": "自动问答转人工待办处理",
                        },
                    )
                )
            alert_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            recent_alerts = [item[2] for item in alert_candidates[:5]]

            stage_meta: dict[str, tuple[str, str]] = {
                "exact_alias": ("Tier 1 精确别名", "good"),
                "decision_preset": ("Tier 1 决策预设", "good"),
                "tier2_short": ("Tier 2 短答", "good"),
                "tier2_long": ("Tier 2 长答引文", "good"),
                "decision_clarify": ("Tier 1 澄清", "ochre"),
                "tier2_clarify": ("Tier 2 澄清", "ochre"),
                "decision_staff": ("决策转人工", "ochre"),
                "tier2_staff": ("检索转人工", "ochre"),
                "staff_case_appended": ("追加已有待办", "muted"),
                "clarify_limit_fallback": ("澄清上限兜底", "ochre"),
                "decision_unknown_fallback": ("超范围兜底", "muted"),
                "tier2_fallback": ("检索无据兜底", "ochre"),
                "tier2_error_fallback": ("检索异常兜底", "urgent"),
                "tier2_unavailable_fallback": ("检索不可用", "urgent"),
            }
            recent_triage_runs: list[dict[str, Any]] = []
            for row in self._conn.execute(
                """
                SELECT run_id, batch_id, clarified_already, stage_path, decision_route,
                       decision_confidence, decision_duration_ms, preset_id, section_title,
                       staff_case_id, created_at_epoch
                FROM triage_runs
                ORDER BY run_id DESC
                LIMIT 15
                """
            ).fetchall():
                stage = str(row["stage_path"] or "")
                label, severity = stage_meta.get(stage, (stage or "未知路径", "muted"))
                ep = float(row["created_at_epoch"])
                conf_val = row["decision_confidence"]
                dur_val = row["decision_duration_ms"]
                recent_triage_runs.append(
                    {
                        "run_id": int(row["run_id"]),
                        "batch_id": int(row["batch_id"]),
                        "time_utc8": _epoch_to_utc8(ep).strftime("%m-%d %H:%M"),
                        "stage_path": stage,
                        "stage_label": label,
                        "severity": severity,
                        "decision_route": str(row["decision_route"] or ""),
                        "decision_confidence": round(float(conf_val), 2) if conf_val is not None else None,
                        "decision_duration_ms": int(dur_val) if dur_val is not None else None,
                        "preset_id": str(row["preset_id"] or ""),
                        "section_title": str(row["section_title"] or ""),
                        "clarified_already": bool(row["clarified_already"]),
                        "staff_case_id": int(row["staff_case_id"]) if row["staff_case_id"] is not None else None,
                    }
                )

            return {
                "snapshot_at_utc8": now_dt.strftime("%Y-%m-%d %H:%M"),
                "snapshot_hm": now_dt.strftime("%H:%M"),
                "date_range_label": f"{day_dts[0].month:02d}/{day_dts[0].day:02d}–{day_dts[-1].month:02d}/{day_dts[-1].day:02d}",
                "daily_inbound": daily_inbound,
                "inbound_7d_total": inbound_7d_total,
                "inbound_7d_avg": inbound_7d_avg,
                "inbound_today_total": inbound_today_total,
                "inbound_all_total": inbound_all_total,
                "inbound_delta_pct": inbound_delta_pct,
                "menu_events_7d": menu_events_7d,
                "menu_events_today": menu_events_today,
                "hourly_inbound_today": hourly_inbound_today,
                "hourly_range_label": hourly_range_label,
                "replies_today": replies_today,
                "replies_all": replies_all,
                "filter_7d": filter_7d,
                "routing_7d": routing_7d,
                "batches": batches,
                "open_cases": open_cases,
                "recent_alerts": recent_alerts,
                "recent_triage_runs": recent_triage_runs,
            }

    def dashboard_librarian_analytics(
        self, now_epoch: float | None = None, include_closed: bool = False
    ) -> dict[str, Any]:
        """Return UTC+8 librarian queue, latency, and feedback analytics without ever exposing reader_id."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_dt = _epoch_to_utc8(now_val)
            today_start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_epoch = today_start_dt.timestamp()
            tomorrow_start_epoch = (today_start_dt + timedelta(days=1)).timestamp()

            open_emerg_rows = self._conn.execute(
                """
                SELECT case_id, account_id, reader_id, case_kind, summary, latest_message, created_at_epoch
                FROM staff_cases
                WHERE status = 'open' AND case_kind = 'emergency'
                ORDER BY created_at_epoch ASC, case_id ASC
                LIMIT 100
                """
            ).fetchall()
            open_todo_rows = self._conn.execute(
                """
                SELECT case_id, account_id, reader_id, case_kind, summary, latest_message, created_at_epoch
                FROM staff_cases
                WHERE status = 'open' AND case_kind <> 'emergency'
                ORDER BY created_at_epoch ASC, case_id ASC
                LIMIT 100
                """
            ).fetchall()

            all_closed_rows = self._conn.execute(
                """
                SELECT case_id, account_id, reader_id, case_kind, summary, latest_message,
                       created_at_epoch, updated_at_epoch, closed_at_utc, resolution_note, closed_by
                FROM staff_cases
                WHERE status = 'closed'
                ORDER BY updated_at_epoch DESC, case_id DESC
                """
            ).fetchall()

            closed_detail_rows = all_closed_rows[:50] if include_closed else []
            msg_case_ids = (
                [int(r["case_id"]) for r in open_emerg_rows]
                + [int(r["case_id"]) for r in open_todo_rows]
                + [int(r["case_id"]) for r in closed_detail_rows]
            )
            messages_by_case: dict[int, list[dict[str, str]]] = {cid: [] for cid in msg_case_ids}
            if msg_case_ids:
                placeholders = ",".join("?" for _ in msg_case_ids)
                msg_rows = self._conn.execute(
                    f"""
                    SELECT case_id, sender_role, content, operator_username, created_at_epoch
                    FROM staff_case_messages
                    WHERE case_id IN ({placeholders})
                    ORDER BY item_id ASC
                    """,
                    msg_case_ids,
                ).fetchall()
                for mrow in msg_rows:
                    cid = int(mrow["case_id"])
                    if cid in messages_by_case:
                        item_msg: dict[str, str] = {
                            "sender_role": str(mrow["sender_role"]),
                            "content": str(mrow["content"]),
                            "created_hm": _epoch_to_utc8(float(mrow["created_at_epoch"])).strftime(
                                "%H:%M"
                            ),
                        }
                        op_user = str(mrow["operator_username"] or "").strip()
                        if op_user:
                            item_msg["operator_username"] = op_user
                        messages_by_case[cid].append(item_msg)

            quota_cache: dict[tuple[str, str], dict[str, Any]] = {}

            def _quota_for(acct: str, reader: str) -> dict[str, Any]:
                key = (acct, reader)
                if key not in quota_cache:
                    quota_cache[key] = self._reader_reply_quota_locked(
                        acct, reader, now_epoch=now_val
                    )
                return dict(quota_cache[key])

            def _build_open_case(row: sqlite3.Row) -> dict[str, Any]:
                cid = int(row["case_id"])
                created_epoch = float(row["created_at_epoch"])
                created_dt = _epoch_to_utc8(created_epoch)
                wait_minutes = max(0, int((now_val - created_epoch) // 60))
                return {
                    "case_id": cid,
                    "case_kind": str(row["case_kind"]),
                    "summary": str(row["summary"]),
                    "latest_message": str(row["latest_message"]),
                    "created_at_utc8": created_dt.strftime("%Y-%m-%d %H:%M"),
                    "created_hm": created_dt.strftime("%H:%M"),
                    "wait_minutes": wait_minutes,
                    "messages": messages_by_case.get(cid, []),
                    "reply_quota": _quota_for(str(row["account_id"]), str(row["reader_id"])),
                }

            open_emergency = [_build_open_case(r) for r in open_emerg_rows]
            open_todo = [_build_open_case(r) for r in open_todo_rows]

            open_counts = {
                "total": len(open_emergency) + len(open_todo),
                "emergency": len(open_emergency),
                "staff_todo": len(open_todo),
            }
            wait_buckets = {
                "emergency": len(open_emergency),
                "over_60m": sum(1 for item in open_todo if item["wait_minutes"] >= 60),
                "m30_60": sum(1 for item in open_todo if 30 <= item["wait_minutes"] < 60),
                "under_30m": sum(1 for item in open_todo if item["wait_minutes"] < 30),
            }

            oldest_candidates: list[tuple[float, int, dict[str, Any]]] = []
            if open_emerg_rows:
                oldest_candidates.append(
                    (
                        float(open_emerg_rows[0]["created_at_epoch"]),
                        int(open_emerg_rows[0]["case_id"]),
                        open_emergency[0],
                    )
                )
            if open_todo_rows:
                oldest_candidates.append(
                    (
                        float(open_todo_rows[0]["created_at_epoch"]),
                        int(open_todo_rows[0]["case_id"]),
                        open_todo[0],
                    )
                )
            if oldest_candidates:
                oldest_candidates.sort(key=lambda item: (item[0], item[1]))
                oldest_item = oldest_candidates[0][2]
                longest_wait = {
                    "minutes": oldest_item["wait_minutes"],
                    "created_hm": oldest_item["created_hm"],
                    "summary": oldest_item["summary"],
                }
            else:
                longest_wait = {"minutes": 0, "created_hm": "", "summary": ""}

            created_by_hour = [0] * 24
            closed_by_hour = [0] * 24
            created_today_rows = self._conn.execute(
                """
                SELECT created_at_epoch
                FROM staff_cases
                WHERE created_at_epoch >= ? AND created_at_epoch < ?
                """,
                (today_start_epoch, tomorrow_start_epoch),
            ).fetchall()
            created_today_count = len(created_today_rows)
            for row in created_today_rows:
                created_by_hour[_epoch_to_utc8(float(row["created_at_epoch"])).hour] += 1

            closed_today_samples: list[tuple[float, int, int]] = []
            for row in all_closed_rows:
                closed_epoch = _parse_closed_epoch(row["closed_at_utc"], float(row["updated_at_epoch"]))
                if today_start_epoch <= closed_epoch < tomorrow_start_epoch:
                    closed_dt = _epoch_to_utc8(closed_epoch)
                    closed_by_hour[closed_dt.hour] += 1
                    duration_min = max(
                        0, int(round((closed_epoch - float(row["created_at_epoch"])) / 60.0))
                    )
                    closed_today_samples.append((closed_epoch, int(row["case_id"]), duration_min))

            closed_today_count = len(closed_today_samples)
            today_stats = {
                "created": created_today_count,
                "closed": closed_today_count,
                "close_rate_pct": (
                    round(closed_today_count / created_today_count * 100.0, 1)
                    if created_today_count > 0
                    else None
                ),
            }

            if closed_today_samples:
                sample_scope = "today"
                closed_today_samples.sort(key=lambda item: (item[0], item[1]))
                sample_durations = [item[2] for item in closed_today_samples]
            else:
                sample_scope = "recent"
                recent_20: list[tuple[float, int, int]] = []
                for row in all_closed_rows[:20]:
                    closed_epoch = _parse_closed_epoch(
                        row["closed_at_utc"], float(row["updated_at_epoch"])
                    )
                    duration_min = max(
                        0, int(round((closed_epoch - float(row["created_at_epoch"])) / 60.0))
                    )
                    recent_20.append((closed_epoch, int(row["case_id"]), duration_min))
                recent_20.sort(key=lambda item: (item[0], item[1]))
                sample_durations = [item[2] for item in recent_20]

            response_stats = {
                "avg_minutes": (
                    round(sum(sample_durations) / len(sample_durations), 1)
                    if sample_durations
                    else None
                ),
                "recent_durations": sample_durations[-6:],
                "sample_count": len(sample_durations),
                "scope": sample_scope,
            }

            le_10m = sum(1 for d in sample_durations if d <= 10)
            m10_20 = sum(1 for d in sample_durations if 10 < d <= 20)
            m20_30 = sum(1 for d in sample_durations if 20 < d <= 30)
            gt_30m = sum(1 for d in sample_durations if d > 30)
            latency_total = len(sample_durations)
            latency_buckets = {
                "le_10m": le_10m,
                "m10_20": m10_20,
                "m20_30": m20_30,
                "gt_30m": gt_30m,
                "total": latency_total,
                "within_30m_pct": (
                    round((le_10m + m10_20 + m20_30) / latency_total * 100.0, 1)
                    if latency_total > 0
                    else None
                ),
                "scope": sample_scope,
            }

            active_hours = [
                h for h in range(24) if created_by_hour[h] > 0 or closed_by_hour[h] > 0
            ]
            start_h = min(9, min(active_hours)) if active_hours else 9
            end_h = max(16, now_dt.hour, max(active_hours)) if active_hours else max(16, now_dt.hour)
            start_h = max(0, min(23, start_h))
            end_h = max(start_h, min(23, end_h))
            hourly_pace_today = [
                {
                    "hour": h,
                    "label": f"{h:02d}:00",
                    "created": created_by_hour[h],
                    "closed": closed_by_hour[h],
                }
                for h in range(start_h, end_h + 1)
            ]
            hourly_range_label = f"{start_h:02d}–{end_h:02d}h"

            recent_closed = []
            for row in all_closed_rows[:15]:
                closed_epoch = _parse_closed_epoch(row["closed_at_utc"], float(row["updated_at_epoch"]))
                closed_dt = _epoch_to_utc8(closed_epoch)
                duration_min = max(
                    0, int(round((closed_epoch - float(row["created_at_epoch"])) / 60.0))
                )
                recent_closed.append(
                    {
                        "case_id": int(row["case_id"]),
                        "case_kind": str(row["case_kind"]),
                        "summary": str(row["summary"]),
                        "closed_at_utc8": closed_dt.strftime("%Y-%m-%d %H:%M"),
                        "closed_hm": closed_dt.strftime("%H:%M"),
                        "resolution_note": str(row["resolution_note"]),
                        "duration_minutes": duration_min,
                    }
                )

            closed_cases: list[dict[str, Any]] = []
            for row in closed_detail_rows:
                cid = int(row["case_id"])
                created_epoch = float(row["created_at_epoch"])
                created_dt = _epoch_to_utc8(created_epoch)
                closed_epoch = _parse_closed_epoch(row["closed_at_utc"], float(row["updated_at_epoch"]))
                closed_dt = _epoch_to_utc8(closed_epoch)
                closed_cases.append(
                    {
                        "case_id": cid,
                        "case_kind": str(row["case_kind"]),
                        "status": "closed",
                        "summary": str(row["summary"]),
                        "latest_message": str(row["latest_message"]),
                        "created_at_utc8": created_dt.strftime("%Y-%m-%d %H:%M"),
                        "created_hm": created_dt.strftime("%H:%M"),
                        "closed_at_utc8": closed_dt.strftime("%Y-%m-%d %H:%M"),
                        "closed_hm": closed_dt.strftime("%H:%M"),
                        "closed_by": str(row["closed_by"] or ""),
                        "resolution_note": str(row["resolution_note"]),
                        "messages": messages_by_case.get(cid, []),
                        "reply_quota": _quota_for(str(row["account_id"]), str(row["reader_id"])),
                    }
                )

            feedback_rows = self._conn.execute(
                """
                SELECT feedback_id, created_at_epoch, content, status, staff_note
                FROM feedback_records
                ORDER BY feedback_id DESC
                LIMIT 20
                """
            ).fetchall()
            recent_feedbacks = [
                {
                    "feedback_id": int(row["feedback_id"]),
                    "created_at_utc8": _epoch_to_utc8(float(row["created_at_epoch"])).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "content": str(row["content"]),
                    "status": str(row["status"]),
                    "staff_note": str(row["staff_note"]),
                }
                for row in feedback_rows
            ]

            return {
                "snapshot_at_utc8": now_dt.strftime("%Y-%m-%d %H:%M"),
                "open_emergency": open_emergency,
                "open_todo": open_todo,
                "closed_cases": closed_cases,
                "open_counts": open_counts,
                "wait_buckets": wait_buckets,
                "longest_wait": longest_wait,
                "today_stats": today_stats,
                "response_stats": response_stats,
                "latency_buckets": latency_buckets,
                "hourly_pace_today": hourly_pace_today,
                "hourly_range_label": hourly_range_label,
                "recent_closed": recent_closed,
                "recent_feedbacks": recent_feedbacks,
            }

    # ------------------------------------------------------------------
    # Dashboard Users & Sessions (RBAC)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_str_list(raw: Any) -> list[str]:
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if isinstance(item, str) and item.strip()]

    def _row_to_dashboard_user(self, row: sqlite3.Row) -> dict[str, Any]:
        perms = normalize_permissions(self._parse_json_str_list(row["permissions_json"]))
        return {
            "user_id": int(row["user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "role": str(row["role"]),
            "permissions": list(perms),
            "is_active": bool(row["is_active"]),
            "created_at_utc": str(row["created_at_utc"]),
            "updated_at_utc": str(row["updated_at_utc"]),
            "last_login_utc": row["last_login_utc"],
        }

    def _delete_user_sessions_locked(self, username: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM dashboard_sessions WHERE username = ?",
            (username,),
        )
        return int(cursor.rowcount)

    def create_dashboard_user(
        self,
        *,
        username: str,
        password: str,
        display_name: str = "",
        role: str = "librarian",
        permissions: Iterable[str] | None = None,
        is_active: bool = True,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Create a dashboard user with PBKDF2-hashed password and normalized permissions."""
        clean_user = username.strip() if isinstance(username, str) else ""
        if not valid_username(clean_user):
            raise ValueError(f"invalid username: {username!r}")
        if not isinstance(password, str) or len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        clean_role = role.strip() if isinstance(role, str) else ""
        if clean_role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")

        if permissions is None:
            norm_perms = (
                ALL_LIBRARIAN_PERMISSIONS if clean_role == "admin" else DEFAULT_LIBRARIAN_PERMISSIONS
            )
        else:
            norm_perms = normalize_permissions(permissions)
        perms_json = json.dumps(list(norm_perms), ensure_ascii=False, separators=(",", ":"))
        pw_hash = hash_password(password)
        clean_display = display_name.strip() if isinstance(display_name, str) else ""

        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            try:
                with self._conn:
                    cursor = self._conn.execute(
                        """
                        INSERT INTO dashboard_users(
                            username, display_name, password_hash, role, permissions_json,
                            is_active, created_at_epoch, created_at_utc,
                            updated_at_epoch, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clean_user,
                            clean_display,
                            pw_hash,
                            clean_role,
                            perms_json,
                            1 if is_active else 0,
                            now_val,
                            now_utc,
                            now_val,
                            now_utc,
                        ),
                    )
                    user_id = int(cursor.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"username already exists: {clean_user}") from exc
            row = self._conn.execute(
                """
                SELECT user_id, username, display_name, role, permissions_json, is_active,
                       created_at_utc, updated_at_utc, last_login_utc
                FROM dashboard_users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_dashboard_user(row)

    def list_dashboard_users(self) -> list[dict[str, Any]]:
        """List all dashboard users without ever exposing password_hash."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT user_id, username, display_name, role, permissions_json, is_active,
                       created_at_utc, updated_at_utc, last_login_utc
                FROM dashboard_users
                ORDER BY user_id ASC
                """
            ).fetchall()
            return [self._row_to_dashboard_user(row) for row in rows]

    def get_dashboard_user(self, username: str) -> dict[str, Any] | None:
        """Fetch one dashboard user by username without exposing password_hash."""
        clean_user = username.strip() if isinstance(username, str) else ""
        if not clean_user:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT user_id, username, display_name, role, permissions_json, is_active,
                       created_at_utc, updated_at_utc, last_login_utc
                FROM dashboard_users
                WHERE username = ?
                """,
                (clean_user,),
            ).fetchone()
            return self._row_to_dashboard_user(row) if row is not None else None

    def update_dashboard_user(
        self,
        username: str,
        *,
        display_name: str | None = None,
        role: str | None = None,
        permissions: Iterable[str] | None = None,
        is_active: bool | None = None,
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Update dashboard user metadata/permissions; disabling a user invalidates their sessions."""
        clean_user = username.strip() if isinstance(username, str) else ""
        if not clean_user:
            return None
        if role is not None:
            clean_role = role.strip() if isinstance(role, str) else ""
            if clean_role not in VALID_ROLES:
                raise ValueError(f"invalid role: {role!r}")
        else:
            clean_role = None

        with self._lock:
            existing = self._conn.execute(
                """
                SELECT user_id, username, display_name, role, permissions_json, is_active
                FROM dashboard_users
                WHERE username = ?
                """,
                (clean_user,),
            ).fetchone()
            if existing is None:
                return None

            new_display = (
                display_name.strip() if isinstance(display_name, str) else str(existing["display_name"])
            )
            new_role = clean_role if clean_role is not None else str(existing["role"])
            if permissions is not None:
                norm_perms = normalize_permissions(permissions)
                new_perms_json = json.dumps(list(norm_perms), ensure_ascii=False, separators=(",", ":"))
            else:
                new_perms_json = str(existing["permissions_json"])
            new_active = (1 if is_active else 0) if is_active is not None else int(existing["is_active"])
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)

            with self._conn:
                self._conn.execute(
                    """
                    UPDATE dashboard_users
                    SET display_name = ?,
                        role = ?,
                        permissions_json = ?,
                        is_active = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?
                    WHERE username = ?
                    """,
                    (new_display, new_role, new_perms_json, new_active, now_val, now_utc, clean_user),
                )
                if is_active is False:
                    self._delete_user_sessions_locked(clean_user)

            row = self._conn.execute(
                """
                SELECT user_id, username, display_name, role, permissions_json, is_active,
                       created_at_utc, updated_at_utc, last_login_utc
                FROM dashboard_users
                WHERE username = ?
                """,
                (clean_user,),
            ).fetchone()
            return self._row_to_dashboard_user(row) if row is not None else None

    def set_dashboard_user_password(
        self,
        username: str,
        new_password: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Reset a dashboard user's password and invalidate all active sessions for that user."""
        if not isinstance(new_password, str) or len(new_password) < 6:
            raise ValueError("password must be at least 6 characters")
        clean_user = username.strip() if isinstance(username, str) else ""
        if not clean_user:
            return False
        pw_hash = hash_password(new_password)
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE dashboard_users
                    SET password_hash = ?, updated_at_epoch = ?, updated_at_utc = ?
                    WHERE username = ?
                    """,
                    (pw_hash, now_val, now_utc, clean_user),
                )
                if cursor.rowcount == 0:
                    return False
                self._delete_user_sessions_locked(clean_user)
                return True

    def verify_dashboard_user(
        self,
        username: str,
        password: str,
        *,
        now_epoch: float | None = None,
    ) -> DashboardPrincipal | None:
        """Authenticate an active DB dashboard user and record last_login_utc on success."""
        clean_user = username.strip() if isinstance(username, str) else ""
        if not clean_user or not isinstance(password, str):
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT user_id, username, display_name, password_hash, role, permissions_json, is_active
                FROM dashboard_users
                WHERE username = ?
                """,
                (clean_user,),
            ).fetchone()
            if row is None or not bool(row["is_active"]):
                return None
            if not verify_password(password, str(row["password_hash"])):
                return None

            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    "UPDATE dashboard_users SET last_login_utc = ? WHERE user_id = ?",
                    (now_utc, int(row["user_id"])),
                )
            role = str(row["role"])
            perms = (
                frozenset(ALL_LIBRARIAN_PERMISSIONS)
                if role == "admin"
                else frozenset(normalize_permissions(self._parse_json_str_list(row["permissions_json"])))
            )
            return DashboardPrincipal(
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                role=role,
                permissions=perms,
                source="db",
            )

    def create_dashboard_session(
        self,
        username: str,
        role: str,
        *,
        ttl_seconds: float = 12 * 3600,
        now_epoch: float | None = None,
    ) -> str:
        """Create a persisted dashboard session and return the raw bearer/cookie token."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        clean_user = username.strip() if isinstance(username, str) else ""
        if not clean_user:
            raise ValueError("username must be non-empty")
        clean_role = role.strip() if isinstance(role, str) else ""
        if clean_role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")

        token = generate_session_token()
        token_hash = hash_session_token(token)
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            expires_at = now_val + float(ttl_seconds)
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO dashboard_sessions(
                        token_hash, username, role, created_at_epoch, expires_at_epoch, last_seen_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (token_hash, clean_user, clean_role, now_val, expires_at, now_val),
                )
        return token

    def get_dashboard_session(
        self, token: str, *, now_epoch: float | None = None
    ) -> dict[str, Any] | None:
        """Look up a session by raw token; deletes and returns None if expired."""
        if not isinstance(token, str) or not token.strip():
            return None
        token_hash = hash_session_token(token)
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            row = self._conn.execute(
                """
                SELECT token_hash, username, role, expires_at_epoch
                FROM dashboard_sessions
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            expires_at = float(row["expires_at_epoch"])
            if expires_at <= now_val:
                with self._conn:
                    self._conn.execute(
                        "DELETE FROM dashboard_sessions WHERE token_hash = ?",
                        (token_hash,),
                    )
                return None
            with self._conn:
                self._conn.execute(
                    "UPDATE dashboard_sessions SET last_seen_epoch = ? WHERE token_hash = ?",
                    (now_val, token_hash),
                )
            return {
                "username": str(row["username"]),
                "role": str(row["role"]),
                "expires_at_epoch": expires_at,
            }

    def delete_dashboard_session(self, token: str) -> None:
        """Delete one session by raw token."""
        if not isinstance(token, str) or not token:
            return
        token_hash = hash_session_token(token)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM dashboard_sessions WHERE token_hash = ?",
                    (token_hash,),
                )

    def delete_user_sessions(self, username: str) -> int:
        """Delete all sessions belonging to a username and return deleted row count."""
        clean_user = username.strip() if isinstance(username, str) else ""
        if not clean_user:
            return 0
        with self._lock:
            with self._conn:
                return self._delete_user_sessions_locked(clean_user)

    def purge_expired_sessions(self, now_epoch: float | None = None) -> int:
        """Delete all expired sessions and return deleted row count."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM dashboard_sessions WHERE expires_at_epoch <= ?",
                    (now_val,),
                )
                return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # Direct Reply Quota & Target Helpers
    # ------------------------------------------------------------------

    def _reader_reply_quota_locked(
        self, account_id: str, reader_id: str, *, now_epoch: float
    ) -> dict[str, Any]:
        acct = (account_id or "default").strip() or "default"
        clean_reader = reader_id.strip() if isinstance(reader_id, str) else ""
        if not clean_reader:
            return {
                "window_open": False,
                "window_remaining_seconds": 0,
                "sent_in_window": 0,
                "quota_limit": WECHAT_INTERACTION_QUOTA_LIMIT,
                "quota_remaining": 0,
                "can_reply": False,
                "blocked_reason": "no_inbound",
            }
        row = self._conn.execute(
            """
            SELECT MAX(received_at_epoch) AS last_inbound
            FROM inbound_events
            WHERE account_id = ? AND reader_id = ?
              AND LOWER(msg_type) <> 'event' AND event = ''
            """,
            (acct, clean_reader),
        ).fetchone()
        if row is None or row["last_inbound"] is None:
            return {
                "window_open": False,
                "window_remaining_seconds": 0,
                "sent_in_window": 0,
                "quota_limit": WECHAT_INTERACTION_QUOTA_LIMIT,
                "quota_remaining": 0,
                "can_reply": False,
                "blocked_reason": "no_inbound",
            }

        last_inbound = float(row["last_inbound"])
        window_remaining_seconds = max(
            0, int((last_inbound + WECHAT_INTERACTION_WINDOW_SECONDS) - now_epoch)
        )
        window_open = window_remaining_seconds > 0
        window_start = max(last_inbound, now_epoch - WECHAT_INTERACTION_WINDOW_SECONDS)
        sent_in_window = int(
            self._conn.execute(
                """
                SELECT COUNT(*)
                FROM outbound_replies
                WHERE account_id = ? AND reader_id = ?
                  AND status = 'sent'
                  AND created_at_epoch >= ?
                """,
                (acct, clean_reader, window_start),
            ).fetchone()[0]
        )
        quota_remaining = max(0, WECHAT_INTERACTION_QUOTA_LIMIT - sent_in_window)
        can_reply = window_open and quota_remaining > 0
        blocked_reason = "" if can_reply else ("window_expired" if not window_open else "quota_exhausted")
        return {
            "window_open": window_open,
            "window_remaining_seconds": window_remaining_seconds,
            "sent_in_window": sent_in_window,
            "quota_limit": WECHAT_INTERACTION_QUOTA_LIMIT,
            "quota_remaining": quota_remaining,
            "can_reply": can_reply,
            "blocked_reason": blocked_reason,
        }

    def get_reader_reply_quota(
        self, account_id: str, reader_id: str, *, now_epoch: float | None = None
    ) -> dict[str, Any]:
        """Compute a reader's WeChat 48h interaction window and 5-message reply quota without returning reader_id."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            return self._reader_reply_quota_locked(account_id, reader_id, now_epoch=now_val)

    def get_staff_case_target(self, case_id: int) -> dict[str, Any] | None:
        """Return internal routing metadata for a staff case (backend-only, contains reader_id)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT case_id, account_id, reader_id, case_kind, status, summary, latest_message
                FROM staff_cases
                WHERE case_id = ?
                """,
                (int(case_id),),
            ).fetchone()
            if row is None:
                return None
            return {
                "case_id": int(row["case_id"]),
                "account_id": str(row["account_id"]),
                "reader_id": str(row["reader_id"]),
                "case_kind": str(row["case_kind"]),
                "status": str(row["status"]),
                "summary": str(row["summary"]),
                "latest_message": str(row["latest_message"]),
            }

    def get_inbox_event_target(self, event_seq: int) -> dict[str, Any] | None:
        """Return internal routing metadata for a non-event reader inbox message (backend-only)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT seq, account_id, reader_id, content, received_at_epoch, staff_case_id
                FROM inbound_events
                WHERE seq = ?
                  AND reader_id <> ''
                  AND LOWER(msg_type) <> 'event'
                  AND event = ''
                """,
                (int(event_seq),),
            ).fetchone()
            if row is None:
                return None
            return {
                "seq": int(row["seq"]),
                "account_id": str(row["account_id"]),
                "reader_id": str(row["reader_id"]),
                "content": str(row["content"]),
                "received_at_epoch": float(row["received_at_epoch"]),
                "staff_case_id": int(row["staff_case_id"]) if row["staff_case_id"] is not None else None,
            }

    def append_staff_case_reply(
        self,
        case_id: int,
        content: str,
        *,
        operator_username: str = "",
        now_epoch: float | None = None,
    ) -> bool:
        """Record a staff reply message on an open or closed staff case."""
        clean_content = content.strip() if isinstance(content, str) else ""
        if not clean_content:
            return False
        clean_operator = operator_username.strip() if isinstance(operator_username, str) else ""
        with self._lock:
            existing = self._conn.execute(
                "SELECT case_id FROM staff_cases WHERE case_id = ?",
                (int(case_id),),
            ).fetchone()
            if existing is None:
                return False
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO staff_case_messages(
                        case_id, sender_role, content, operator_username, created_at_epoch, created_at_utc
                    ) VALUES (?, 'staff', ?, ?, ?, ?)
                    """,
                    (int(case_id), clean_content, clean_operator, now_val, now_utc),
                )
                self._conn.execute(
                    """
                    UPDATE staff_cases
                    SET updated_at_epoch = ?, updated_at_utc = ?
                    WHERE case_id = ?
                    """,
                    (now_val, now_utc, int(case_id)),
                )
            return True

    # ------------------------------------------------------------------
    # Preset Drafts CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_string_sequence(items: Sequence[str] | Iterable[str], *, deduplicate: bool = False) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, str):
                continue
            val = item.strip()
            if not val:
                continue
            if deduplicate:
                if val in seen:
                    continue
                seen.add(val)
            cleaned.append(val)
        return cleaned

    def _row_to_preset_draft(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "draft_id": int(row["draft_id"]),
            "source_case_id": int(row["source_case_id"]) if row["source_case_id"] is not None else None,
            "source_event_seq": int(row["source_event_seq"]) if row["source_event_seq"] is not None else None,
            "source_question": str(row["source_question"]),
            "staff_reply": str(row["staff_reply"]),
            "mode": str(row["mode"]),
            "target_preset_id": str(row["target_preset_id"]),
            "preset_id": str(row["preset_id"]),
            "title": str(row["title"]),
            "aliases": self._parse_json_str_list(row["aliases_json"]),
            "applicability": str(row["applicability"]),
            "body": str(row["body"]),
            "ai_generation_note": str(row["ai_generation_note"]),
            "ai_review_status": str(row["ai_review_status"]),
            "ai_review_reasons": self._parse_json_str_list(row["ai_review_reasons_json"]),
            "ai_review_suggestion": str(row["ai_review_suggestion"]),
            "status": str(row["status"]),
            "created_by": str(row["created_by"]),
            "reviewed_by": str(row["reviewed_by"]),
            "created_at_epoch": float(row["created_at_epoch"]),
            "created_at_utc": str(row["created_at_utc"]),
            "updated_at_epoch": float(row["updated_at_epoch"]),
            "updated_at_utc": str(row["updated_at_utc"]),
            "published_at_utc": row["published_at_utc"],
        }

    def create_preset_draft(
        self,
        *,
        source_case_id: int | None = None,
        source_event_seq: int | None = None,
        source_question: str = "",
        staff_reply: str = "",
        mode: str = "create",
        target_preset_id: str = "",
        preset_id: str = "",
        title: str = "",
        aliases: Sequence[str] = (),
        applicability: str = "",
        body: str = "",
        ai_generation_note: str = "",
        created_by: str = "",
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Persist a newly generated `1.` preset template draft."""
        clean_mode = mode.strip() if isinstance(mode, str) else "create"
        if clean_mode not in ("create", "merge"):
            raise ValueError(f"invalid draft mode: {mode!r}")
        aliases_list = self._clean_string_sequence(aliases, deduplicate=True)
        aliases_json = json.dumps(aliases_list, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO preset_drafts(
                        source_case_id, source_event_seq, source_question, staff_reply,
                        mode, target_preset_id, preset_id, title, aliases_json,
                        applicability, body, ai_generation_note,
                        ai_review_status, ai_review_reasons_json, ai_review_suggestion,
                        status, created_by, reviewed_by,
                        created_at_epoch, created_at_utc, updated_at_epoch, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '[]', '', 'draft', ?, '', ?, ?, ?, ?)
                    """,
                    (
                        source_case_id,
                        source_event_seq,
                        source_question.strip(),
                        staff_reply.strip(),
                        clean_mode,
                        target_preset_id.strip(),
                        preset_id.strip(),
                        title.strip(),
                        aliases_json,
                        applicability.strip(),
                        body.strip(),
                        ai_generation_note.strip(),
                        created_by.strip(),
                        now_val,
                        now_utc,
                        now_val,
                        now_utc,
                    ),
                )
                draft_id = int(cursor.lastrowid)
            row = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_preset_draft(row)

    def get_preset_draft(self, draft_id: int) -> dict[str, Any] | None:
        """Fetch one preset draft by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            return self._row_to_preset_draft(row) if row is not None else None

    def list_preset_drafts(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List preset drafts ordered by most recently updated."""
        bounded = max(1, min(limit, 200))
        where_sql = "WHERE status = ?" if status else ""
        params: tuple[Any, ...] = (status, bounded) if status else (bounded,)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM preset_drafts
                {where_sql}
                ORDER BY updated_at_epoch DESC, draft_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_preset_draft(row) for row in rows]

    def update_preset_draft(
        self,
        draft_id: int,
        *,
        mode: str | None = None,
        target_preset_id: str | None = None,
        preset_id: str | None = None,
        title: str | None = None,
        aliases: Sequence[str] | None = None,
        applicability: str | None = None,
        body: str | None = None,
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Edit preset draft fields and reset ai_review_status to 'pending' whenever content changes."""
        if mode is not None:
            clean_mode = mode.strip() if isinstance(mode, str) else ""
            if clean_mode not in ("create", "merge"):
                raise ValueError(f"invalid draft mode: {mode!r}")
        else:
            clean_mode = None

        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            if existing is None:
                return None

            new_mode = clean_mode if clean_mode is not None else str(existing["mode"])
            new_target = (
                target_preset_id.strip() if target_preset_id is not None else str(existing["target_preset_id"])
            )
            new_preset_id = preset_id.strip() if preset_id is not None else str(existing["preset_id"])
            new_title = title.strip() if title is not None else str(existing["title"])
            if aliases is not None:
                new_aliases_list = self._clean_string_sequence(aliases, deduplicate=True)
                new_aliases_json = json.dumps(new_aliases_list, ensure_ascii=False, separators=(",", ":"))
            else:
                new_aliases_json = str(existing["aliases_json"])
            new_applicability = (
                applicability.strip() if applicability is not None else str(existing["applicability"])
            )
            new_body = body.strip() if body is not None else str(existing["body"])

            content_changed = (
                new_mode != str(existing["mode"])
                or new_target != str(existing["target_preset_id"])
                or new_preset_id != str(existing["preset_id"])
                or new_title != str(existing["title"])
                or new_aliases_json != str(existing["aliases_json"])
                or new_applicability != str(existing["applicability"])
                or new_body != str(existing["body"])
            )
            new_review_status = "pending" if content_changed else str(existing["ai_review_status"])
            new_review_reasons = "[]" if content_changed else str(existing["ai_review_reasons_json"])
            new_review_suggestion = "" if content_changed else str(existing["ai_review_suggestion"])

            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE preset_drafts
                    SET mode = ?,
                        target_preset_id = ?,
                        preset_id = ?,
                        title = ?,
                        aliases_json = ?,
                        applicability = ?,
                        body = ?,
                        ai_review_status = ?,
                        ai_review_reasons_json = ?,
                        ai_review_suggestion = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?
                    WHERE draft_id = ?
                    """,
                    (
                        new_mode,
                        new_target,
                        new_preset_id,
                        new_title,
                        new_aliases_json,
                        new_applicability,
                        new_body,
                        new_review_status,
                        new_review_reasons,
                        new_review_suggestion,
                        now_val,
                        now_utc,
                        int(draft_id),
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            return self._row_to_preset_draft(row) if row is not None else None

    def set_preset_draft_review(
        self,
        draft_id: int,
        *,
        ai_review_status: str,
        reasons: Sequence[str],
        suggestion: str,
        reviewed_by: str = "",
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Persist deterministic/AI review outcome on a preset draft."""
        clean_status = ai_review_status.strip() if isinstance(ai_review_status, str) else ""
        if not clean_status:
            raise ValueError("ai_review_status must be non-empty")
        reasons_json = json.dumps(
            self._clean_string_sequence(reasons), ensure_ascii=False, separators=(",", ":")
        )
        clean_suggestion = suggestion.strip() if isinstance(suggestion, str) else ""
        clean_reviewer = reviewed_by.strip() if isinstance(reviewed_by, str) else ""
        with self._lock:
            existing = self._conn.execute(
                "SELECT reviewed_by FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            if existing is None:
                return None
            final_reviewer = clean_reviewer or str(existing["reviewed_by"])
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE preset_drafts
                    SET ai_review_status = ?,
                        ai_review_reasons_json = ?,
                        ai_review_suggestion = ?,
                        reviewed_by = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?
                    WHERE draft_id = ?
                    """,
                    (
                        clean_status,
                        reasons_json,
                        clean_suggestion,
                        final_reviewer,
                        now_val,
                        now_utc,
                        int(draft_id),
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            return self._row_to_preset_draft(row) if row is not None else None

    def mark_preset_draft_published(
        self,
        draft_id: int,
        *,
        reviewed_by: str = "",
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Mark a preset draft as published and record published_at_utc."""
        clean_reviewer = reviewed_by.strip() if isinstance(reviewed_by, str) else ""
        with self._lock:
            existing = self._conn.execute(
                "SELECT reviewed_by FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            if existing is None:
                return None
            final_reviewer = clean_reviewer or str(existing["reviewed_by"])
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE preset_drafts
                    SET status = 'published',
                        reviewed_by = ?,
                        published_at_utc = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?
                    WHERE draft_id = ?
                    """,
                    (final_reviewer, now_utc, now_val, now_utc, int(draft_id)),
                )
            row = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            return self._row_to_preset_draft(row) if row is not None else None

    def reject_preset_draft(
        self,
        draft_id: int,
        *,
        reviewed_by: str = "",
        reason: str = "",
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        """Mark a preset draft as rejected and optionally append a rejection reason."""
        clean_reviewer = reviewed_by.strip() if isinstance(reviewed_by, str) else ""
        clean_reason = reason.strip() if isinstance(reason, str) else ""
        with self._lock:
            existing = self._conn.execute(
                "SELECT reviewed_by, ai_review_reasons_json FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            if existing is None:
                return None
            final_reviewer = clean_reviewer or str(existing["reviewed_by"])
            reasons = self._parse_json_str_list(existing["ai_review_reasons_json"])
            if clean_reason and clean_reason not in reasons:
                reasons.append(clean_reason)
            reasons_json = json.dumps(reasons, ensure_ascii=False, separators=(",", ":"))
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE preset_drafts
                    SET status = 'rejected',
                        reviewed_by = ?,
                        ai_review_reasons_json = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?
                    WHERE draft_id = ?
                    """,
                    (final_reviewer, reasons_json, now_val, now_utc, int(draft_id)),
                )
            row = self._conn.execute(
                "SELECT * FROM preset_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            return self._row_to_preset_draft(row) if row is not None else None
