"""Authenticated self-hosted receiver for testing WeChat Cloud Hosting message forwarding."""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse


_AUTH_TOKEN = os.environ.get("RELAY_AUTH_TOKEN", "")
_LOG_PATH = Path(os.environ.get("RELAY_LOG_PATH", "data/relay-events.jsonl"))
MAX_BODY_BYTES = 512 * 1024
_MAX_RECENT_EVENTS = 200
_HEADERS = {"Cache-Control": "no-store"}
_LOGGER = logging.getLogger("uvicorn.error")


def _safe_token(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", "."})[:64]


class RelayEventStore:
    """Thread-safe JSONL store with MsgId deduplication for forwarded WeChat callbacks."""

    def __init__(self, log_path: Path, *, max_recent: int = _MAX_RECENT_EVENTS) -> None:
        self.log_path = log_path
        self._max_recent = max_recent
        self._lock = threading.Lock()
        self._next_seq = 0
        self._seen_msg_ids: dict[str, int] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=max_recent)
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.log_path.exists():
            return
        with self.log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                seq = item.get("seq")
                if type(seq) is int and seq > self._next_seq:
                    self._next_seq = seq
                msg_id = item.get("msg_id")
                if isinstance(msg_id, str) and msg_id and type(seq) is int:
                    self._seen_msg_ids.setdefault(msg_id, seq)
                self._recent.append(item)

    def _ensure_secure_parent(self) -> None:
        parent = self.log_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)

    def record_event(self, payload: dict[str, Any], *, client_ip: str) -> tuple[bool, int]:
        """Persist a forwarded callback once per non-empty MsgId and return (duplicate, seq)."""
        raw_msg_id = payload.get("msg_id")
        msg_id = raw_msg_id if isinstance(raw_msg_id, str) else ""
        with self._lock:
            if msg_id and msg_id in self._seen_msg_ids:
                return True, self._seen_msg_ids[msg_id]

            self._next_seq += 1
            seq = self._next_seq
            record = {
                "seq": seq,
                "relay_received_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "client_ip": client_ip,
                "schema_version": payload.get("schema_version"),
                "transport": payload.get("transport") if isinstance(payload.get("transport"), str) else "",
                "received_at_utc": (
                    payload.get("received_at_utc") if isinstance(payload.get("received_at_utc"), str) else ""
                ),
                "content_type": (
                    payload.get("content_type") if isinstance(payload.get("content_type"), str) else ""
                ),
                "msg_type": payload.get("msg_type") if isinstance(payload.get("msg_type"), str) else "",
                "msg_id": msg_id or None,
                "from_user": payload.get("from_user") if isinstance(payload.get("from_user"), str) else "",
                "to_user": payload.get("to_user") if isinstance(payload.get("to_user"), str) else "",
                "create_time": (
                    payload.get("create_time") if isinstance(payload.get("create_time"), str) else ""
                ),
                "content": payload.get("content") if isinstance(payload.get("content"), str) else "",
                "event": payload.get("event") if isinstance(payload.get("event"), str) else "",
                "wx_headers": payload.get("wx_headers") if isinstance(payload.get("wx_headers"), dict) else {},
                "raw_body": payload.get("raw_body") if isinstance(payload.get("raw_body"), str) else "",
            }

            self._ensure_secure_parent()
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            if msg_id:
                self._seen_msg_ids[msg_id] = seq
            self._recent.append(record)
            return False, seq

    def recent_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return up to `limit` newest stored events."""
        bounded = max(1, min(limit, self._max_recent))
        with self._lock:
            items = list(self._recent)
        items.reverse()
        return items[:bounded]


def _verify_bearer_token(request: Request) -> None:
    if not _AUTH_TOKEN:
        raise HTTPException(503, "relay authentication is not configured")
    scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not supplied
        or not hmac.compare_digest(supplied, _AUTH_TOKEN)
    ):
        raise HTTPException(
            401,
            "invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _get_store(application: FastAPI) -> RelayEventStore:
    store = getattr(application.state, "relay_store", None)
    if store is None:
        store = RelayEventStore(_LOG_PATH)
        application.state.relay_store = store
    return store


@asynccontextmanager
async def _lifespan(application: FastAPI):
    if not _AUTH_TOKEN:
        raise RuntimeError("RELAY_AUTH_TOKEN must be set before starting the relay receiver")
    application.state.relay_store = RelayEventStore(_LOG_PATH)
    _LOGGER.info("Relay receiver ready: log_path=%s", _LOG_PATH)
    yield


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    redirect_slashes=False,
    lifespan=_lifespan,
)


@app.get("/healthz", response_class=PlainTextResponse)
async def health() -> PlainTextResponse:
    return PlainTextResponse("ok", headers=_HEADERS)


@app.post("/relay/wechat", response_class=JSONResponse)
async def receive_forwarded_wechat(request: Request) -> JSONResponse:
    _verify_bearer_token(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "message exceeds 512 KiB")
        body.extend(chunk)

    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(422, "JSON object required")

    msg_type = payload.get("msg_type")
    if not isinstance(msg_type, str) or not msg_type.strip():
        raise HTTPException(422, "msg_type must be a non-empty string")

    client_ip = request.client.host if request.client else ""
    store = _get_store(request.app)
    duplicate, seq = store.record_event(payload, client_ip=client_ip)

    wx_headers = payload.get("wx_headers")
    wx_source = ""
    if isinstance(wx_headers, dict) and isinstance(wx_headers.get("x-wx-source"), str):
        wx_source = _safe_token(wx_headers["x-wx-source"])
    transport = payload.get("transport") if isinstance(payload.get("transport"), str) else "unknown"
    _LOGGER.info(
        "Relay event accepted: seq=%d msg_type=%s transport=%s duplicate=%s wx_source=%s",
        seq,
        _safe_token(msg_type) or "unknown",
        _safe_token(transport) or "unknown",
        str(duplicate).lower(),
        wx_source or "-",
    )
    return JSONResponse(
        {"ok": True, "accepted": True, "duplicate": duplicate, "seq": seq},
        headers=_HEADERS,
    )


@app.get("/relay/events", response_class=JSONResponse)
async def list_forwarded_events(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    _verify_bearer_token(request)
    store = _get_store(request.app)
    events = store.recent_events(limit=limit)
    return JSONResponse({"count": len(events), "events": events}, headers=_HEADERS)
