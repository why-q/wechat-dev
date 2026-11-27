"""Shared lightweight parsing, UTF-8 bounding, and log-redaction helpers."""

from __future__ import annotations

import os


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable safely."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    """Parse a strictly positive float environment variable with fallback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def env_int(name: str, default: int) -> int:
    """Parse a non-negative integer environment variable with fallback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def parse_allowlist(raw: str) -> frozenset[str]:
    """Parse comma/semicolon/newline-separated identifiers into a stripped set."""
    normalized = raw.replace(";", ",").replace("\n", ",")
    return frozenset(token.strip() for token in normalized.split(",") if token.strip())


def safe_token(value: str, *, allow_colon: bool = False) -> str:
    """Sanitize a short operational token before writing it to logs."""
    allowed = {"-", "_", "."} | ({":"} if allow_colon else set())
    return "".join(ch for ch in value if ch.isalnum() or ch in allowed)[:64]


def safe_errmsg(value: object) -> str:
    """Redact non-ASCII text from external API error strings before logging/storing."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in " .,:;-_/()")) else " " for ch in value
    )
    return " ".join(cleaned.split())[:96]


def truncate_utf8(text: str, byte_limit: int) -> str:
    """Truncate text cleanly at UTF-8 character boundaries within byte_limit."""
    if byte_limit <= 0:
        return ""
    if len(text.encode("utf-8")) <= byte_limit:
        return text
    ellipsis = "…"
    remaining = max(0, byte_limit - len(ellipsis.encode("utf-8")))
    prefix: list[str] = []
    for character in text:
        size = len(character.encode("utf-8"))
        if size > remaining:
            break
        prefix.append(character)
        remaining -= size
    return "".join(prefix) + ellipsis if byte_limit >= 3 else "".join(prefix)
