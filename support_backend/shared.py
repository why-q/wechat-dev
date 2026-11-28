"""Shared lightweight parsing, UTF-8 bounding, and log-redaction helpers."""

from __future__ import annotations

import json
import os
import re
from typing import Any

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _scan_json_dicts(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder(strict=False)
    found: list[dict[str, Any]] = []
    pos = 0
    length = len(text)
    while pos < length:
        brace = text.find("{", pos)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
            pos = end
        else:
            pos = brace + 1
    return found


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Robustly extract a JSON object from LLM output (tolerates <think>, fences, prose, control chars, trailing commas)."""
    cleaned = _THINK_BLOCK_RE.sub("", str(raw_text or "")).strip()
    if not cleaned:
        raise ValueError("empty LLM output")

    # Fast path: clean JSON object (e.g. native JSON mode).
    try:
        direct = json.loads(cleaned, strict=False)
        if isinstance(direct, dict):
            return direct
    except json.JSONDecodeError:
        pass

    # Candidate sources: fenced blocks first (in reverse order), then full cleaned text.
    candidates: list[str] = []
    fenced = [m.group(1).strip() for m in _FENCED_BLOCK_RE.finditer(cleaned) if m.group(1).strip()]
    candidates.extend(reversed(fenced))
    candidates.append(cleaned)

    for candidate in candidates:
        dicts = _scan_json_dicts(candidate)
        if dicts:
            return dicts[-1]
        repaired = _TRAILING_COMMA_RE.sub(r"\1", candidate)
        if repaired != candidate:
            dicts = _scan_json_dicts(repaired)
            if dicts:
                return dicts[-1]

    raise ValueError("no valid JSON object found")


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
