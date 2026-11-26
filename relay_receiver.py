"""Authenticated self-hosted receiver and outbound reply tester for WeChat Cloud Hosting relay."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from support_backend import (
    DEFAULT_DECISION_MODEL,
    DEFAULT_DECISION_TIMEOUT_SECONDS,
    DEFAULT_LONG_ANSWER_CHARS,
    DEFAULT_MAX_PREEMPTS,
    DEFAULT_MAX_WAIT_SECONDS,
    DEFAULT_MERGE_ENABLED,
    DEFAULT_MIN_SCORE,
    DEFAULT_PRESET_MIN_CONFIDENCE,
    DEFAULT_QUIET_SECONDS,
    DEFAULT_TOP_K,
    PresetCatalog,
    SupportPipeline,
    SupportResponder,
    SupportStore,
    SystemOneClient,
    Tier2KnowledgeResponder,
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


_AUTH_TOKEN = os.environ.get("RELAY_AUTH_TOKEN", "")
_LOG_PATH = Path(os.environ.get("RELAY_LOG_PATH", "data/relay-events.jsonl"))
_SUPPORT_DB_PATH_RAW = os.environ.get("SUPPORT_DB_PATH", "").strip()
_SUPPORT_QUIET_SECONDS = _env_float("SUPPORT_QUIET_SECONDS", DEFAULT_QUIET_SECONDS)
_SUPPORT_MAX_WAIT_SECONDS = _env_float("SUPPORT_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS)
_SUPPORT_MERGE_ENABLED = _env_bool("SUPPORT_MERGE_ENABLED", default=DEFAULT_MERGE_ENABLED)
_SUPPORT_IMMEDIATE_MAX_PREEMPTS = _env_int(
    "SUPPORT_IMMEDIATE_MAX_PREEMPTS", DEFAULT_MAX_PREEMPTS
)
_SUPPORT_AUTO_REPLY_ENABLED = _env_bool("SUPPORT_AUTO_REPLY_ENABLED", default=False)
_SUPPORT_CORPUS_DIR_RAW = os.environ.get("SUPPORT_CORPUS_DIR", "rag-poc/corpus").strip()
_SUPPORT_INDEX_DIR_RAW = os.environ.get("SUPPORT_INDEX_DIR", "rag-poc/index_v2").strip()
_SUPPORT_DECISION_BASE_URL = os.environ.get("SUPPORT_DECISION_BASE_URL", "").strip()
_SUPPORT_DECISION_API_KEY = os.environ.get("SUPPORT_DECISION_API_KEY", "").strip()
_SUPPORT_DECISION_MODEL = (
    os.environ.get("SUPPORT_DECISION_MODEL", DEFAULT_DECISION_MODEL).strip()
    or DEFAULT_DECISION_MODEL
)
_SUPPORT_DECISION_TIMEOUT_SECONDS = _env_float(
    "SUPPORT_DECISION_TIMEOUT_SECONDS", DEFAULT_DECISION_TIMEOUT_SECONDS
)
_SUPPORT_PRESET_MIN_CONFIDENCE = _env_float(
    "SUPPORT_PRESET_MIN_CONFIDENCE", DEFAULT_PRESET_MIN_CONFIDENCE
)
_SUPPORT_LONG_ANSWER_CHARS = _env_int("SUPPORT_LONG_ANSWER_CHARS", DEFAULT_LONG_ANSWER_CHARS)
_SUPPORT_TIER2_ENABLED = _env_bool("SUPPORT_TIER2_ENABLED", default=True)
_SUPPORT_TIER2_TOP_K = _env_int("SUPPORT_TIER2_TOP_K", DEFAULT_TOP_K) or DEFAULT_TOP_K
_SUPPORT_TIER2_MIN_SCORE = _env_float("SUPPORT_TIER2_MIN_SCORE", DEFAULT_MIN_SCORE)
_APP_ID = os.environ.get("WECHAT_APP_ID", "")
_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")
_DIRECT_ECHO_TEXT = os.environ.get("RELAY_DIRECT_ECHO_TEXT", "").strip()
_CLOUD_ECHO_TEXT = os.environ.get("RELAY_CLOUD_ECHO_TEXT", "").strip()

MAX_BODY_BYTES = 512 * 1024
MAX_CUSTOMER_MESSAGE_BYTES = 1800
MAX_TEST_REPLY_CHARS = 2000
_MAX_RECENT_EVENTS = 200
_HTTP_TIMEOUT_SECONDS = 5
_ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 60
_HEADERS = {"Cache-Control": "no-store"}
_LOGGER = logging.getLogger("uvicorn.error")


def _safe_token(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", "."})[:64]


def _safe_errmsg(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in " .,:;-_/()")) else " " for ch in value
    )
    return " ".join(cleaned.split())[:96]


def _truncate_utf8(text: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    if len(text.encode("utf-8")) <= byte_limit:
        return text
    ellipsis = "…"
    remaining = max(0, byte_limit - len(ellipsis.encode("utf-8")))
    prefix = []
    for character in text:
        size = len(character.encode("utf-8"))
        if size > remaining:
            break
        prefix.append(character)
        remaining -= size
    return "".join(prefix) + ellipsis if byte_limit >= 3 else "".join(prefix)


class WeChatCredentialsMissingError(RuntimeError):
    """Raised when direct WeChat API credentials are unavailable."""


class WeChatTransportError(RuntimeError):
    """Raised when a direct WeChat API request fails without a valid errcode."""


def _request_json(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = URLRequest(url, data=data, headers=headers)
    with urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("WeChat API returned a non-object response")
    return result


class _WeChatDirectClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0

    def reset_cache(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def _get_access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            if not _APP_ID or not _APP_SECRET:
                raise WeChatCredentialsMissingError("WeChat API credentials are unavailable")
            query = urlencode({
                "grant_type": "client_credential",
                "appid": _APP_ID,
                "secret": _APP_SECRET,
            })
            try:
                result = _request_json(f"https://api.weixin.qq.com/cgi-bin/token?{query}")
            except Exception as exc:
                raise WeChatTransportError(type(exc).__name__) from None
            errcode = result.get("errcode", 0)
            if errcode != 0:
                raise WeChatTransportError(
                    f"token_errcode_{errcode}" if type(errcode) is int else "token_rejected"
                )
            token = result.get("access_token")
            expires_in = result.get("expires_in")
            if not isinstance(token, str) or not token or type(expires_in) is not int or expires_in <= 0:
                raise WeChatTransportError("token_incomplete")
            self._access_token = token
            self._expires_at = time.monotonic() + max(
                expires_in - _ACCESS_TOKEN_REFRESH_MARGIN_SECONDS, 1
            )
            return token

    def send_text(self, to_user: str, content: str) -> dict[str, Any]:
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "text": {"content": _truncate_utf8(content, MAX_CUSTOMER_MESSAGE_BYTES)},
        }
        started_at = time.monotonic()
        for attempt in range(2):
            token = self._get_access_token(force_refresh=(attempt > 0))
            query = urlencode({"access_token": token})
            try:
                result = _request_json(
                    f"https://api.weixin.qq.com/cgi-bin/message/custom/send?{query}",
                    payload,
                )
            except Exception as exc:
                raise WeChatTransportError(type(exc).__name__) from None
            errcode = result.get("errcode")
            if type(errcode) is not int:
                raise WeChatTransportError("invalid_errcode")
            if errcode == 40001 and attempt == 0:
                self.reset_cache()
                continue
            return {
                "ok": errcode == 0,
                "errcode": errcode,
                "errmsg": _safe_errmsg(result.get("errmsg")),
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                "mode": "direct_https",
            }
        raise WeChatTransportError("retry_exhausted")


_DIRECT_CLIENT = _WeChatDirectClient()


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
        if isinstance(raw_msg_id, str):
            msg_id = raw_msg_id
        elif type(raw_msg_id) is int:
            msg_id = str(raw_msg_id)
        else:
            msg_id = ""
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

    def get_event(self, seq: int) -> dict[str, Any] | None:
        """Return the stored event matching `seq`, scanning disk if evicted from cache."""
        with self._lock:
            for item in self._recent:
                if item.get("seq") == seq:
                    return dict(item)
            if not self.log_path.exists():
                return None
            with self.log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        item = json.loads(stripped)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get("seq") == seq:
                        return item
        return None


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


def _get_support_pipeline(application: FastAPI) -> SupportPipeline | None:
    pipeline = getattr(application.state, "support_pipeline", None)
    return pipeline if isinstance(pipeline, SupportPipeline) else None


def _require_support_pipeline(application: FastAPI) -> SupportPipeline:
    pipeline = _get_support_pipeline(application)
    if pipeline is None:
        raise HTTPException(503, "support pipeline is not initialized")
    return pipeline


async def _run_direct_echo(seq: int, to_user: str, text: str) -> None:
    try:
        result = await asyncio.to_thread(_DIRECT_CLIENT.send_text, to_user, text)
    except WeChatCredentialsMissingError:
        _LOGGER.warning("Relay direct echo skipped: seq=%d reason=missing_credentials", seq)
        return
    except WeChatTransportError as exc:
        _LOGGER.warning("Relay direct echo failed: seq=%d reason=%s", seq, str(exc) or "transport_error")
        return
    _LOGGER.info(
        "Relay direct echo finished: seq=%d ok=%s errcode=%d errmsg=%s duration_ms=%d",
        seq,
        str(result["ok"]).lower(),
        result["errcode"],
        result["errmsg"] or "-",
        result["duration_ms"],
    )


async def _read_json_object(request: Request) -> dict[str, Any]:
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
    return payload


@asynccontextmanager
async def _lifespan(application: FastAPI):
    if not _AUTH_TOKEN:
        raise RuntimeError("RELAY_AUTH_TOKEN must be set before starting the relay receiver")
    application.state.relay_store = RelayEventStore(_LOG_PATH)
    db_path = (
        Path(_SUPPORT_DB_PATH_RAW)
        if _SUPPORT_DB_PATH_RAW
        else (_LOG_PATH.parent / "library-support.db")
    )
    max_wait_seconds = max(_SUPPORT_MAX_WAIT_SECONDS, _SUPPORT_QUIET_SECONDS)
    support_store = SupportStore(
        db_path,
        quiet_seconds=_SUPPORT_QUIET_SECONDS,
        max_wait_seconds=max_wait_seconds,
        merge_enabled=_SUPPORT_MERGE_ENABLED,
        max_preempts=_SUPPORT_IMMEDIATE_MAX_PREEMPTS,
    )
    preset_catalog = PresetCatalog.from_corpus_dir(Path(_SUPPORT_CORPUS_DIR_RAW))

    decision_client: SystemOneClient | None = None
    if _SUPPORT_DECISION_BASE_URL and _SUPPORT_DECISION_API_KEY:
        try:
            decision_client = SystemOneClient(
                base_url=_SUPPORT_DECISION_BASE_URL,
                api_key=_SUPPORT_DECISION_API_KEY,
                model=_SUPPORT_DECISION_MODEL,
                timeout_seconds=_SUPPORT_DECISION_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            _LOGGER.warning("Decision router config invalid: reason=%s", str(exc))

    knowledge_responder: Tier2KnowledgeResponder | None = None
    index_dir = Path(_SUPPORT_INDEX_DIR_RAW)
    if _SUPPORT_TIER2_ENABLED and index_dir.is_dir() and any(index_dir.iterdir()):
        try:
            knowledge_responder = Tier2KnowledgeResponder(
                index_dir=index_dir,
                top_k=_SUPPORT_TIER2_TOP_K,
                min_score=_SUPPORT_TIER2_MIN_SCORE,
                long_threshold=_SUPPORT_LONG_ANSWER_CHARS,
            )
        except Exception as exc:
            frames = traceback.extract_tb(exc.__traceback__)
            loc = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "-"
            _LOGGER.warning(
                "Tier 2 knowledge initialization skipped: reason=%s loc=%s",
                getattr(exc, "code", type(exc).__name__),
                loc,
            )

    def _clarify_with_tier2_llm(question: str, system_prompt: str) -> str:
        if knowledge_responder is None:
            return ""
        from llama_index.core.base.llms.types import ChatMessage, MessageRole

        resp = knowledge_responder._llm.chat(
            [
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                ChatMessage(role=MessageRole.USER, content=question),
            ]
        )
        return str(getattr(resp.message, "content", None) or resp)

    responder = SupportResponder(
        store=support_store,
        preset_catalog=preset_catalog,
        decision_client=decision_client,
        knowledge_responder=knowledge_responder,
        clarify_generator=_clarify_with_tier2_llm if knowledge_responder is not None else None,
        preset_min_confidence=_SUPPORT_PRESET_MIN_CONFIDENCE,
    )
    pipeline = SupportPipeline(
        support_store,
        send_text_fn=_DIRECT_CLIENT.send_text,
        auto_reply_enabled=_SUPPORT_AUTO_REPLY_ENABLED,
        triage_fn=responder.triage,
    )
    pipeline.commit_handler = lambda outcome, plan: responder.apply_committed_plan(
        outcome, plan, deliver_reply_fn=pipeline.deliver_reply
    )
    application.state.support_responder = responder
    application.state.support_pipeline = pipeline
    await pipeline.start()
    _LOGGER.info(
        "Relay receiver ready: log_path=%s db_path=%s merge_enabled=%s max_preempts=%d presets=%d decision_ready=%s tier2_ready=%s has_direct_creds=%s auto_reply=%s direct_echo=%s cloud_echo=%s",
        _LOG_PATH,
        db_path,
        str(_SUPPORT_MERGE_ENABLED).lower(),
        _SUPPORT_IMMEDIATE_MAX_PREEMPTS,
        len(preset_catalog.presets),
        str(decision_client is not None).lower(),
        str(knowledge_responder is not None).lower(),
        str(bool(_APP_ID and _APP_SECRET)).lower(),
        str(_SUPPORT_AUTO_REPLY_ENABLED).lower(),
        str(bool(_DIRECT_ECHO_TEXT)).lower(),
        str(bool(_CLOUD_ECHO_TEXT)).lower(),
    )
    try:
        yield
    finally:
        await pipeline.stop()
        support_store.close()
        application.state.support_responder = None
        application.state.support_pipeline = None


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
async def receive_forwarded_wechat(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    _verify_bearer_token(request)
    payload = await _read_json_object(request)

    msg_type = payload.get("msg_type")
    if not isinstance(msg_type, str) or not msg_type.strip():
        raise HTTPException(422, "msg_type must be a non-empty string")

    client_ip = request.client.host if request.client else ""
    store = _get_store(request.app)
    duplicate, seq = store.record_event(payload, client_ip=client_ip)
    pipeline = _get_support_pipeline(request.app)
    if pipeline is not None:
        ingest_result = pipeline.ingest_sync(payload, client_ip=client_ip)
        background_tasks.add_task(pipeline.process_ingest_side_effects, ingest_result, payload)

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

    from_user = payload.get("from_user") if isinstance(payload.get("from_user"), str) else ""
    if not duplicate and msg_type == "text" and _DIRECT_ECHO_TEXT and from_user:
        background_tasks.add_task(_run_direct_echo, seq, from_user, _DIRECT_ECHO_TEXT)

    response_payload: dict[str, Any] = {
        "ok": True,
        "accepted": True,
        "duplicate": duplicate,
        "seq": seq,
    }
    if not duplicate and msg_type == "text" and _CLOUD_ECHO_TEXT:
        response_payload["cloud_test_reply"] = _CLOUD_ECHO_TEXT
    return JSONResponse(response_payload, headers=_HEADERS)


@app.get("/relay/events", response_class=JSONResponse)
async def list_forwarded_events(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    _verify_bearer_token(request)
    store = _get_store(request.app)
    events = store.recent_events(limit=limit)
    return JSONResponse({"count": len(events), "events": events}, headers=_HEADERS)


@app.post("/relay/send-test", response_class=JSONResponse)
async def send_test_reply(request: Request) -> JSONResponse:
    _verify_bearer_token(request)
    payload = await _read_json_object(request)

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(422, "text must be a non-empty string")
    if len(text) > MAX_TEST_REPLY_CHARS:
        raise HTTPException(413, "text is too long")

    seq = payload.get("seq")
    to_user = payload.get("to_user")
    resolved_seq: int | None = None
    if seq is not None:
        if type(seq) is not int or seq < 1:
            raise HTTPException(422, "seq must be a positive integer")
        event = _get_store(request.app).get_event(seq)
        if event is None:
            raise HTTPException(404, "event not found")
        target_user = event.get("from_user")
        if not isinstance(target_user, str) or not target_user:
            raise HTTPException(422, "event does not contain from_user")
        resolved_seq = seq
    elif isinstance(to_user, str) and to_user.strip():
        target_user = to_user.strip()
    else:
        raise HTTPException(422, "either seq or to_user is required")

    try:
        result = await asyncio.to_thread(_DIRECT_CLIENT.send_text, target_user, text)
    except WeChatCredentialsMissingError:
        raise HTTPException(503, "WeChat API credentials are not configured") from None
    except WeChatTransportError as exc:
        _LOGGER.warning(
            "Relay send-test transport error: seq=%s reason=%s",
            resolved_seq if resolved_seq is not None else "-",
            str(exc) or "transport_error",
        )
        raise HTTPException(502, {"code": "wechat_api_transport_error", "reason": str(exc)}) from None

    _LOGGER.info(
        "Relay send-test finished: seq=%s ok=%s errcode=%d errmsg=%s duration_ms=%d",
        resolved_seq if resolved_seq is not None else "-",
        str(result["ok"]).lower(),
        result["errcode"],
        result["errmsg"] or "-",
        result["duration_ms"],
    )
    return JSONResponse({"seq": resolved_seq, **result}, headers=_HEADERS)


@app.get("/relay/support/overview", response_class=JSONResponse)
async def support_overview(request: Request) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    responder = getattr(request.app.state, "support_responder", None)
    return JSONResponse(
        {
            "auto_reply_enabled": pipeline.auto_reply_enabled,
            "preset_count": len(responder.preset_catalog.presets) if responder is not None else 0,
            "decision_router_ready": bool(responder is not None and responder.decision_client is not None),
            "tier2_ready": bool(responder is not None and responder.knowledge_responder is not None),
            **pipeline.store.overview_metrics(),
        },
        headers=_HEADERS,
    )


@app.get("/relay/support/events", response_class=JSONResponse)
async def list_support_events(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    category: str | None = Query(default=None),
) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    events = pipeline.store.list_events(limit=limit, category=category)
    return JSONResponse({"count": len(events), "events": events}, headers=_HEADERS)


@app.get("/relay/support/batches", response_class=JSONResponse)
async def list_support_batches(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    batches = pipeline.store.list_batches(limit=limit, status=status)
    return JSONResponse({"count": len(batches), "batches": batches}, headers=_HEADERS)


@app.get("/relay/support/feedbacks", response_class=JSONResponse)
async def list_support_feedbacks(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    feedbacks = pipeline.store.list_feedbacks(limit=limit)
    return JSONResponse({"count": len(feedbacks), "feedbacks": feedbacks}, headers=_HEADERS)


@app.get("/relay/support/cases", response_class=JSONResponse)
async def list_support_cases(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    cases = pipeline.store.list_staff_cases(limit=limit, status=status)
    return JSONResponse({"count": len(cases), "cases": cases}, headers=_HEADERS)


@app.post("/relay/support/cases", response_class=JSONResponse)
async def create_support_case(request: Request) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    payload = await _read_json_object(request)
    reader_id = payload.get("reader_id")
    summary = payload.get("summary")
    account_id = payload.get("account_id") if isinstance(payload.get("account_id"), str) else "default"
    if not isinstance(reader_id, str) or not reader_id.strip():
        raise HTTPException(422, "reader_id must be a non-empty string")
    if not isinstance(summary, str) or not summary.strip():
        raise HTTPException(422, "summary must be a non-empty string")
    latest_message = (
        payload.get("latest_message").strip()
        if isinstance(payload.get("latest_message"), str)
        else ""
    )
    created = pipeline.store.create_staff_case(
        account_id=account_id.strip() or "default",
        reader_id=reader_id.strip(),
        summary=summary.strip(),
        latest_message=latest_message,
    )
    return JSONResponse({"case": created.to_dict()}, headers=_HEADERS)


@app.post("/relay/support/cases/{case_id}/close", response_class=JSONResponse)
async def close_support_case(request: Request, case_id: int) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    raw = await request.body()
    payload: dict[str, Any] = {}
    if raw.strip():
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "message exceeds 512 KiB")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise HTTPException(400, "invalid JSON") from None
        if not isinstance(decoded, dict):
            raise HTTPException(422, "JSON object required")
        payload = decoded
    resolution_note = payload.get("resolution_note") if isinstance(payload.get("resolution_note"), str) else ""
    closed = pipeline.store.close_staff_case(case_id, resolution_note=resolution_note)
    if closed is None:
        raise HTTPException(404, "staff case not found")
    return JSONResponse({"case": closed.to_dict()}, headers=_HEADERS)


@app.get("/relay/support/replies", response_class=JSONResponse)
async def list_support_replies(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    replies = pipeline.store.list_outbound_replies(limit=limit)
    return JSONResponse({"count": len(replies), "replies": replies}, headers=_HEADERS)


@app.post("/relay/support/flush-due", response_class=JSONResponse)
async def flush_due_support_batches(request: Request) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    outcomes = await pipeline.flush_due_now()
    return JSONResponse(
        {
            "flushed_count": len(outcomes),
            "outcomes": [item.to_dict() for item in outcomes],
        },
        headers=_HEADERS,
    )


@app.get("/relay/support/triage-runs", response_class=JSONResponse)
async def list_support_triage_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> JSONResponse:
    _verify_bearer_token(request)
    pipeline = _require_support_pipeline(request.app)
    runs = pipeline.store.list_triage_runs(limit=limit)
    return JSONResponse({"count": len(runs), "runs": runs}, headers=_HEADERS)


@app.get("/relay/support/presets", response_class=JSONResponse)
async def list_support_presets(request: Request) -> JSONResponse:
    _verify_bearer_token(request)
    _require_support_pipeline(request.app)
    responder = getattr(request.app.state, "support_responder", None)
    presets = (
        [
            {
                "preset_id": item.preset_id,
                "title": item.title,
                "aliases": list(item.aliases),
                "applicability": item.applicability,
            }
            for item in responder.preset_catalog.presets
        ]
        if responder is not None
        else []
    )
    return JSONResponse({"count": len(presets), "presets": presets}, headers=_HEADERS)
