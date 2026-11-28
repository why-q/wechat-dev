"""Authentication, password hashing, session tokens, and RBAC permission helpers."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
from typing import Iterable

PERM_VIEW_WORKBENCH = "view_workbench"
PERM_REPLY_READER = "reply_reader"
PERM_CLOSE_CASE = "close_case"
PERM_DRAFT_PRESET = "draft_preset"
PERM_REVIEW_PRESET = "review_preset"

ALL_LIBRARIAN_PERMISSIONS: tuple[str, ...] = (
    PERM_VIEW_WORKBENCH,
    PERM_REPLY_READER,
    PERM_CLOSE_CASE,
    PERM_DRAFT_PRESET,
    PERM_REVIEW_PRESET,
)

DEFAULT_LIBRARIAN_PERMISSIONS: tuple[str, ...] = (
    PERM_VIEW_WORKBENCH,
    PERM_REPLY_READER,
    PERM_CLOSE_CASE,
    PERM_DRAFT_PRESET,
)

PERMISSION_LABELS: dict[str, str] = {
    PERM_VIEW_WORKBENCH: "查看工作台",
    PERM_REPLY_READER: "回复读者",
    PERM_CLOSE_CASE: "工单结案",
    PERM_DRAFT_PRESET: "生成模板草稿",
    PERM_REVIEW_PRESET: "审核发布模板",
}

VALID_ROLES: tuple[str, ...] = ("admin", "librarian")

_PBKDF2_SCHEME = "pbkdf2_sha256"
_DEFAULT_PBKDF2_ITERATIONS = 200_000
_USERNAME_RE = re.compile(r"[a-zA-Z0-9_.-]{2,32}")


@dataclass(frozen=True, slots=True)
class DashboardPrincipal:
    """Authenticated dashboard operator identity and permission set."""

    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    source: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def has_permission(self, perm: str) -> bool:
        if self.is_admin:
            return True
        return perm in self.permissions


def hash_password(password: str, *, iterations: int = _DEFAULT_PBKDF2_ITERATIONS) -> str:
    """Hash a plaintext password using salted PBKDF2-HMAC-SHA256."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    if not isinstance(iterations, int) or iterations < 1:
        raise ValueError("iterations must be a positive integer")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"{_PBKDF2_SCHEME}${iterations}${salt_b64}${digest_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored PBKDF2 hash in constant time."""
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    scheme, iter_raw, salt_b64, digest_b64 = parts
    if scheme != _PBKDF2_SCHEME:
        return False
    try:
        iterations = int(iter_raw)
    except ValueError:
        return False
    if iterations < 1:
        return False
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_b64.encode("ascii"), validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return False
    if not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_session_token() -> str:
    """Generate a high-entropy URL-safe session token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Compute the SHA-256 hex digest of a session token for database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_permissions(raw: Iterable[str]) -> tuple[str, ...]:
    """Filter, deduplicate, and canonically order librarian permission keys."""
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            seen.add(item.strip())
    return tuple(perm for perm in ALL_LIBRARIAN_PERMISSIONS if perm in seen)


def valid_username(value: str) -> bool:
    """Return True iff value is a valid dashboard username."""
    return isinstance(value, str) and _USERNAME_RE.fullmatch(value) is not None
