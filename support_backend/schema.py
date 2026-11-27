"""SQLite schema DDL, serializable snapshots, and coercion helpers for SupportStore."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from .filters import FilterDecision, default_same_case_matcher

SCHEMA_VERSION = 3


def utc_iso(epoch_seconds: float) -> str:
    """Format a UTC timestamp as an ISO-8601 second-resolution string."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds")


def coerce_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def coerce_msg_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def coerce_headers_json(value: Any) -> str:
    if not isinstance(value, dict):
        return "{}"
    cleaned = {
        str(k): str(v)
        for k, v in value.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def default_same_case_adapter(
    merged_text: str, open_case: dict[str, Any], now_epoch: float
) -> tuple[bool, str]:
    return default_same_case_matcher(merged_text, open_case, now_epoch=now_epoch)


@dataclass(frozen=True, slots=True)
class StaffCaseSnapshot:
    """Serializable summary of an open or closed staff case / to-do."""

    case_id: int
    account_id: str
    reader_id: str
    case_kind: str
    status: str
    summary: str
    latest_message: str
    created_at_utc: str
    updated_at_utc: str
    updated_at_epoch: float
    closed_at_utc: str | None = None
    resolution_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "account_id": self.account_id,
            "reader_id": self.reader_id,
            "case_kind": self.case_kind,
            "status": self.status,
            "summary": self.summary,
            "latest_message": self.latest_message,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "updated_at_epoch": self.updated_at_epoch,
            "closed_at_utc": self.closed_at_utc,
            "resolution_note": self.resolution_note,
        }


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
    """Current state of one merge batch."""

    batch_id: int
    account_id: str
    reader_id: str
    status: str
    first_received_at_epoch: float
    last_received_at_epoch: float
    quiet_deadline_epoch: float
    max_deadline_epoch: float
    flush_at_epoch: float
    message_count: int
    merged_text: str | None
    flushed_at_epoch: float | None
    route_stage: str
    staff_case_id: int | None
    generation: int = 1
    preempt_count: int = 0
    preempt_locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "account_id": self.account_id,
            "reader_id": self.reader_id,
            "status": self.status,
            "first_received_at_epoch": self.first_received_at_epoch,
            "last_received_at_epoch": self.last_received_at_epoch,
            "quiet_deadline_epoch": self.quiet_deadline_epoch,
            "max_deadline_epoch": self.max_deadline_epoch,
            "flush_at_epoch": self.flush_at_epoch,
            "message_count": self.message_count,
            "merged_text": self.merged_text,
            "flushed_at_epoch": self.flushed_at_epoch,
            "route_stage": self.route_stage,
            "staff_case_id": self.staff_case_id,
            "generation": self.generation,
            "preempt_count": self.preempt_count,
            "preempt_locked": self.preempt_locked,
        }


@dataclass(frozen=True, slots=True)
class FlushOutcome:
    """Result returned whenever a merge batch finishes flushing."""

    batch: BatchSnapshot
    event_seqs: tuple[int, ...]
    route_stage: str
    staff_case_id: int | None = None
    same_case_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": self.batch.to_dict(),
            "event_seqs": list(self.event_seqs),
            "route_stage": self.route_stage,
            "staff_case_id": self.staff_case_id,
            "same_case_reason": self.same_case_reason,
        }


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of recording an inbound event and running Filter + Merge enqueue."""

    duplicate: bool
    seq: int
    received_at_epoch: float
    filter_decision: FilterDecision
    batch: BatchSnapshot | None = None
    feedback_id: int | None = None
    staff_case_id: int | None = None
    pre_flushed: tuple[FlushOutcome, ...] = ()
    preempted_batch_id: int | None = None
    queued_behind_batch_id: int | None = None


@dataclass(frozen=True, slots=True)
class ClarificationSnapshot:
    """Active or historical single-turn clarification record for a reader."""

    clarify_id: int
    account_id: str
    reader_id: str
    status: str
    original_batch_id: int | None
    original_question: str
    clarify_prompt: str
    attempt_count: int
    asked_at_epoch: float
    asked_at_utc: str
    expires_at_epoch: float
    consumed_at_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clarify_id": self.clarify_id,
            "account_id": self.account_id,
            "reader_id": self.reader_id,
            "status": self.status,
            "original_batch_id": self.original_batch_id,
            "original_question": self.original_question,
            "clarify_prompt": self.clarify_prompt,
            "attempt_count": self.attempt_count,
            "asked_at_epoch": self.asked_at_epoch,
            "asked_at_utc": self.asked_at_utc,
            "expires_at_epoch": self.expires_at_epoch,
            "consumed_at_utc": self.consumed_at_utc,
        }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_records (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    event_seq INTEGER,
    created_at_epoch REAL NOT NULL,
    created_at_utc TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded',
    staff_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS staff_cases (
    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    source_batch_id INTEGER,
    source_event_seq INTEGER,
    case_kind TEXT NOT NULL DEFAULT 'staff_todo',
    status TEXT NOT NULL DEFAULT 'open',
    summary TEXT NOT NULL,
    latest_message TEXT NOT NULL,
    created_at_epoch REAL NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_epoch REAL NOT NULL,
    updated_at_utc TEXT NOT NULL,
    closed_at_utc TEXT,
    resolution_note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_staff_cases_open_reader
    ON staff_cases(account_id, reader_id, status, updated_at_epoch);

CREATE TABLE IF NOT EXISTS merge_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    status TEXT NOT NULL,
    first_received_at_epoch REAL NOT NULL,
    last_received_at_epoch REAL NOT NULL,
    quiet_deadline_epoch REAL NOT NULL,
    max_deadline_epoch REAL NOT NULL,
    flush_at_epoch REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    merged_text TEXT,
    flushed_at_epoch REAL,
    route_stage TEXT NOT NULL DEFAULT 'pending',
    staff_case_id INTEGER REFERENCES staff_cases(case_id),
    generation INTEGER NOT NULL DEFAULT 1,
    preempt_count INTEGER NOT NULL DEFAULT 0,
    preempt_locked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_merge_status_flush
    ON merge_batches(status, flush_at_epoch);

CREATE TABLE IF NOT EXISTS inbound_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE,
    transport TEXT NOT NULL,
    received_at_epoch REAL NOT NULL,
    received_at_utc TEXT NOT NULL,
    create_time TEXT NOT NULL,
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    event TEXT NOT NULL,
    msg_id TEXT,
    content TEXT NOT NULL,
    filter_category TEXT NOT NULL,
    filter_reason TEXT NOT NULL,
    batch_id INTEGER REFERENCES merge_batches(batch_id),
    staff_case_id INTEGER REFERENCES staff_cases(case_id),
    feedback_id INTEGER REFERENCES feedback_records(feedback_id),
    client_ip TEXT NOT NULL,
    wx_headers_json TEXT NOT NULL,
    raw_body TEXT NOT NULL,
    expires_at_epoch REAL
);

CREATE INDEX IF NOT EXISTS idx_inbound_reader_time
    ON inbound_events(account_id, reader_id, received_at_epoch);

CREATE INDEX IF NOT EXISTS idx_inbound_category
    ON inbound_events(filter_category, seq);

CREATE TABLE IF NOT EXISTS batch_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES merge_batches(batch_id),
    event_seq INTEGER NOT NULL UNIQUE REFERENCES inbound_events(seq),
    received_at_epoch REAL NOT NULL,
    text_content TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_messages_order
    ON batch_messages(batch_id, received_at_epoch, event_seq);

CREATE TABLE IF NOT EXISTS staff_case_messages (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES staff_cases(case_id),
    batch_id INTEGER REFERENCES merge_batches(batch_id),
    event_seq INTEGER REFERENCES inbound_events(seq),
    sender_role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at_epoch REAL NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_replies (
    reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_ref_id INTEGER,
    content TEXT NOT NULL,
    delivery_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    errcode INTEGER,
    errmsg TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    created_at_epoch REAL NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clarification_states (
    clarify_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    status TEXT NOT NULL,
    original_batch_id INTEGER REFERENCES merge_batches(batch_id),
    original_question TEXT NOT NULL,
    clarify_prompt TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    asked_at_epoch REAL NOT NULL,
    asked_at_utc TEXT NOT NULL,
    expires_at_epoch REAL NOT NULL,
    consumed_at_utc TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_clarify_active_reader
    ON clarification_states(account_id, reader_id)
    WHERE status = 'awaiting_reply';

CREATE TABLE IF NOT EXISTS triage_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES merge_batches(batch_id),
    account_id TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    clarified_already INTEGER NOT NULL DEFAULT 0,
    stage_path TEXT NOT NULL,
    decision_route TEXT NOT NULL DEFAULT '',
    decision_preset_id TEXT NOT NULL DEFAULT '',
    decision_confidence REAL,
    decision_duration_ms INTEGER,
    preset_id TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    section_title TEXT NOT NULL DEFAULT '',
    reply_preview TEXT NOT NULL DEFAULT '',
    staff_case_id INTEGER REFERENCES staff_cases(case_id),
    created_at_epoch REAL NOT NULL,
    created_at_utc TEXT NOT NULL
);
"""
