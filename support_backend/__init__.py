"""Self-hosted backend package for library WeChat support pipeline."""

from .db import (
    BatchSnapshot,
    DEFAULT_MAX_PREEMPTS,
    DEFAULT_MAX_WAIT_SECONDS,
    DEFAULT_MERGE_ENABLED,
    DEFAULT_QUIET_SECONDS,
    FlushOutcome,
    IngestResult,
    StaffCaseSnapshot,
    SupportStore,
)
from .filters import (
    EMERGENCY_CONTACT_REPLY,
    FEEDBACK_RECEIVED_REPLY,
    FilterDecision,
    classify_message,
    default_same_case_matcher,
)
from .pipeline import SupportPipeline

__all__ = [
    "BatchSnapshot",
    "DEFAULT_MAX_PREEMPTS",
    "DEFAULT_MAX_WAIT_SECONDS",
    "DEFAULT_MERGE_ENABLED",
    "DEFAULT_QUIET_SECONDS",
    "EMERGENCY_CONTACT_REPLY",
    "FEEDBACK_RECEIVED_REPLY",
    "FilterDecision",
    "FlushOutcome",
    "IngestResult",
    "StaffCaseSnapshot",
    "SupportPipeline",
    "SupportStore",
    "classify_message",
    "default_same_case_matcher",
]
