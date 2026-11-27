"""SQLite WAL persistence layer for WeChat library support intake, merging, and staff cases."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable

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
        now_epoch: float | None = None,
    ) -> StaffCaseSnapshot | None:
        """Mark a staff case resolved on the Dashboard so later questions take normal flow."""
        with self._lock:
            now_val = float(self._clock() if now_epoch is None else now_epoch)
            now_utc = _utc_iso(now_val)
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE staff_cases
                    SET status = 'closed',
                        closed_at_utc = ?,
                        updated_at_epoch = ?,
                        updated_at_utc = ?,
                        resolution_note = ?
                    WHERE case_id = ? AND status = 'open'
                    """,
                    (now_utc, now_val, now_utc, resolution_note.strip(), case_id),
                )
                if cursor.rowcount > 0 and resolution_note.strip():
                    self._conn.execute(
                        """
                        INSERT INTO staff_case_messages(
                            case_id, sender_role, content, created_at_epoch, created_at_utc
                        ) VALUES (?, 'staff', ?, ?, ?)
                        """,
                        (case_id, resolution_note.strip(), now_val, now_utc),
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
                        created_at_epoch, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        """Return retained reader messages for display-only librarian inbox pages."""
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
                SELECT seq, received_at_utc, msg_type, content, filter_category
                FROM inbound_events
                WHERE {' AND '.join(clauses)}
                ORDER BY seq DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

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
                    SELECT item_id, batch_id, event_seq, sender_role, content, created_at_utc
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
                       content, delivery_mode, status, errcode, errmsg, duration_ms, created_at_utc
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
