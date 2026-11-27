"""WeChat callback with citation-grounded LlamaIndex replies."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request as URLRequest, urlopen

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if parsed < min_value or parsed > max_value:
        return max(min_value, min(max_value, parsed))
    return parsed


def _validate_forward_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("WECHAT_FORWARD_URL must be a valid http or https URL without credentials")
    return cleaned


def _resolve_runtime_modes(forward_url: str, token: str) -> tuple[bool, bool, bool]:
    """Derive relay-only defaults when forwarding to an external backend."""
    relay_only = bool(forward_url)
    cloud_push = _env_flag("WECHAT_CLOUD_PUSH_ENABLED", relay_only and not token)
    auto_reply = _env_flag("WECHAT_AUTO_REPLY_ENABLED", not relay_only)
    dashboard = _env_flag("DASHBOARD_ENABLED", not relay_only)
    return cloud_push, auto_reply, dashboard


_TOKEN = os.environ.get("WECHAT_TOKEN", "")
_APP_ID = os.environ.get("WECHAT_APP_ID", "")
_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")
_FORWARD_URL = _validate_forward_url(os.environ.get("WECHAT_FORWARD_URL", ""))
_FORWARD_TOKEN = os.environ.get("WECHAT_FORWARD_TOKEN", "")
_FORWARD_TIMEOUT_SECONDS = _env_float(
    "WECHAT_FORWARD_TIMEOUT_SECONDS", 3.0, min_value=0.2, max_value=4.5
)
_FORWARD_FAIL_OPEN = _env_flag("WECHAT_FORWARD_FAIL_OPEN", True)
_FORWARD_ALLOW_TEST_REPLY = _env_flag("WECHAT_FORWARD_ALLOW_TEST_REPLY", False)
_CLOUD_PUSH_ENABLED, _AUTO_REPLY_ENABLED, _DASHBOARD_ENABLED = _resolve_runtime_modes(
    _FORWARD_URL, _TOKEN
)
_CLOUD_EXPECTED_SOURCE = os.environ.get("WECHAT_CLOUD_EXPECTED_SOURCE", "").strip()
_CLOUD_EXPECTED_ENV = os.environ.get("WECHAT_CLOUD_EXPECTED_ENV", "").strip()
_CLOUD_EXPECTED_APPID = os.environ.get("WECHAT_CLOUD_EXPECTED_APPID", "").strip()
_WECHAT_OPEN_API_ENABLED = _env_flag("WECHAT_OPEN_API_ENABLED", False)
_RAG_DEBUG_TOKEN = os.environ.get("RAG_DEBUG_TOKEN", "")
if not _TOKEN and not _CLOUD_PUSH_ENABLED:
    raise RuntimeError("WECHAT_TOKEN must be set unless Cloud Hosting push mode is enabled")

MAX_BODY_BYTES = 256 * 1024
_HEADERS = {"Cache-Control": "no-store"}
_LOGGER = logging.getLogger("uvicorn.error")
_HTTP_TIMEOUT_SECONDS = 5

@asynccontextmanager
async def _lifespan(application: FastAPI):
    application.state.rag_responder = None
    if _FORWARD_URL:
        header_checks = sum(
            1
            for item in (_CLOUD_EXPECTED_SOURCE, _CLOUD_EXPECTED_ENV, _CLOUD_EXPECTED_APPID)
            if item
        )
        _LOGGER.info(
            "WeChat callback forwarder enabled: timeout_s=%.2f fail_open=%s has_token=%s allow_test_reply=%s cloud_header_checks=%d",
            _FORWARD_TIMEOUT_SECONDS,
            str(_FORWARD_FAIL_OPEN).lower(),
            str(bool(_FORWARD_TOKEN)).lower(),
            str(_FORWARD_ALLOW_TEST_REPLY).lower(),
            header_checks,
        )
    if _AUTO_REPLY_ENABLED:
        try:
            from rag_poc.cli import RagResponder

            application.state.rag_responder = RagResponder()
        except Exception as exc:
            code, _, error_type = _rag_failure_details(exc, "rag_initialization_failed")
            _LOGGER.error(
                "RAG startup failed: code=%s error_type=%s; text callbacks unavailable",
                code,
                error_type,
            )
    yield


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    redirect_slashes=False,
    lifespan=_lifespan,
)


MAX_MESSAGES = 100
MAX_CUSTOMER_MESSAGE_BYTES = 1800
_RAG_ERROR_REPLY = "抱歉，我暂时无法处理这个问题，请稍后再试。"
MAX_RAG_DEBUG_QUESTION_CHARS = 2000
_ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 60
_ACCESS_TOKEN_LOCK = threading.Lock()
_ACCESS_TOKEN: str | None = None
_ACCESS_TOKEN_EXPIRES_AT = 0.0
_MESSAGE_LOCK = threading.Lock()


def _rag_failure_details(exc: Exception, fallback_code: str) -> tuple[str, str, str]:
    code = getattr(exc, "code", None)
    message = getattr(exc, "public_message", None)
    cause_type = getattr(exc, "cause_type", None)
    if isinstance(code, str) and isinstance(message, str):
        return code, message, cause_type or type(exc).__name__
    return fallback_code, "RAG 查询遇到未分类错误。", type(exc).__name__


# Self-contained UTF-8 bounding helper (app.py runs standalone inside the Cloud Hosting image).
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


def _limit_customer_message(text: str) -> str:
    if len(text.encode("utf-8")) <= MAX_CUSTOMER_MESSAGE_BYTES:
        return text
    marker = "\n\n可核对来源：\n"
    if marker not in text:
        return _truncate_utf8(text, MAX_CUSTOMER_MESSAGE_BYTES)
    answer, reference_text = text.split(marker, 1)
    references = reference_text.splitlines()
    reference_budget = MAX_CUSTOMER_MESSAGE_BYTES // 2
    per_reference = max(32, reference_budget // max(1, len(references)))
    shortened = []
    for reference in references:
        if len(reference.encode("utf-8")) <= per_reference:
            shortened.append(reference)
            continue
        label, separator, excerpt = reference.partition(" — ")
        if not separator:
            shortened.append(_truncate_utf8(reference, per_reference))
            continue
        citation = re.match(r"\[\d+\]", label)
        citation_prefix = citation.group(0) if citation else ""
        safe_label = _truncate_utf8(label, per_reference - 3)
        if citation_prefix and not safe_label.startswith(citation_prefix):
            safe_label = _truncate_utf8(citation_prefix, per_reference)
            shortened.append(safe_label)
            continue
        excerpt_budget = max(
            0,
            per_reference - len((safe_label + separator).encode("utf-8")),
        )
        shortened.append(safe_label + separator + _truncate_utf8(excerpt, excerpt_budget))
    footer = marker + "\n".join(shortened)
    answer_budget = max(
        0,
        MAX_CUSTOMER_MESSAGE_BYTES - len(footer.encode("utf-8")) - 3,
    )
    return _truncate_utf8(answer, answer_budget) + footer


@dataclass
class _RecentMessage:
    record_id: int
    timestamp: str
    text: str
    reply: str
    reply_status: str
    message_id: str | None


_MESSAGES: deque[_RecentMessage] = deque(maxlen=MAX_MESSAGES)
_MESSAGE_IDS: set[str] = set()
_NEXT_RECORD_ID = 0


def _store_message(text: str, message_id: str | None) -> int | None:
    global _NEXT_RECORD_ID
    with _MESSAGE_LOCK:
        if message_id and message_id in _MESSAGE_IDS:
            return None
        if len(_MESSAGES) == MAX_MESSAGES:
            evicted = _MESSAGES[0]
            if evicted.message_id:
                _MESSAGE_IDS.discard(evicted.message_id)
        _NEXT_RECORD_ID += 1
        record = _RecentMessage(
            record_id=_NEXT_RECORD_ID,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            text=text,
            reply="",
            reply_status="pending",
            message_id=message_id or None,
        )
        _MESSAGES.append(record)
        if record.message_id:
            _MESSAGE_IDS.add(record.message_id)
        return record.record_id


def _set_message_result(record_id: int, reply: str, status: str) -> None:
    with _MESSAGE_LOCK:
        for record in _MESSAGES:
            if record.record_id == record_id:
                record.reply = reply
                record.reply_status = status
                return

def _recent_messages() -> list[dict[str, str]]:
    with _MESSAGE_LOCK:
        return [
            {
                "timestamp": item.timestamp,
                "text": item.text,
                "reply": item.reply,
                "reply_status": item.reply_status,
            }
            for item in reversed(_MESSAGES)
        ]


def _request_json(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = URLRequest(url, data=data, headers=headers)
    with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("WeChat API returned a non-object response")
    return result


def _get_access_token() -> str:
    global _ACCESS_TOKEN, _ACCESS_TOKEN_EXPIRES_AT
    with _ACCESS_TOKEN_LOCK:
        if _ACCESS_TOKEN and time.monotonic() < _ACCESS_TOKEN_EXPIRES_AT:
            return _ACCESS_TOKEN
        if not _APP_ID or not _APP_SECRET:
            raise ValueError("WeChat API credentials are unavailable")
        query = urlencode({
            "grant_type": "client_credential",
            "appid": _APP_ID,
            "secret": _APP_SECRET,
        })
        result = _request_json(f"https://api.weixin.qq.com/cgi-bin/token?{query}")
        if result.get("errcode", 0) != 0:
            raise ValueError("WeChat access token request was rejected")
        token = result.get("access_token")
        expires_in = result.get("expires_in")
        if not isinstance(token, str) or not token or type(expires_in) is not int or expires_in <= 0:
            raise ValueError("WeChat access token response was incomplete")
        _ACCESS_TOKEN = token
        _ACCESS_TOKEN_EXPIRES_AT = time.monotonic() + max(
            expires_in - _ACCESS_TOKEN_REFRESH_MARGIN_SECONDS, 1
        )
        return token


def _safe_errmsg(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in " .,:;-_/()")) else " " for ch in value)
    return " ".join(cleaned.split())[:96]


def _send_customer_message(to_user: str, content: str) -> bool:
    payload = {
        "touser": to_user,
        "msgtype": "text",
        "text": {"content": _limit_customer_message(content)},
    }
    mode = "cloud_open_api" if _WECHAT_OPEN_API_ENABLED else "access_token_api"
    request_started_at = time.monotonic()
    _LOGGER.info("WeChat customer message request started: mode=%s", mode)
    try:
        if _WECHAT_OPEN_API_ENABLED:
            result = _request_json(
                "http://api.weixin.qq.com/cgi-bin/message/custom/send", payload
            )
        else:
            access_token = _get_access_token()
            query = urlencode({"access_token": access_token})
            result = _request_json(
                f"https://api.weixin.qq.com/cgi-bin/message/custom/send?{query}",
                payload,
            )
    except Exception as exc:
        _LOGGER.warning(
            "WeChat delayed reply failed: reason=api_request_error mode=%s error_type=%s duration_ms=%d",
            mode,
            type(exc).__name__,
            round((time.monotonic() - request_started_at) * 1000),
        )
        return False
    errcode = result.get("errcode")
    if type(errcode) is not int or errcode != 0:
        duration_ms = round((time.monotonic() - request_started_at) * 1000)
        if type(errcode) is int:
            errmsg = _safe_errmsg(result.get("errmsg"))
            _LOGGER.warning(
                "WeChat delayed reply failed: mode=%s errcode=%d errmsg=%s duration_ms=%d",
                mode,
                errcode,
                errmsg or "-",
                duration_ms,
            )
        else:
            _LOGGER.warning(
                "WeChat delayed reply failed: reason=invalid_errcode mode=%s duration_ms=%d",
                mode,
                duration_ms,
            )
        return False
    _LOGGER.info(
        "WeChat customer message API accepted: errcode=0 mode=%s duration_ms=%d",
        mode,
        round((time.monotonic() - request_started_at) * 1000),
    )
    return True


async def _answer_then_send(
    responder, question: str, to_user: str, record_id: int
) -> None:
    started_at = time.monotonic()
    _LOGGER.info("WeChat reply task started: record_id=%d", record_id)
    try:
        _LOGGER.info("RAG reply generation started: record_id=%d", record_id)
        try:
            reply = _limit_customer_message(
                await asyncio.to_thread(responder.answer, question)
            )
        except Exception as exc:
            code, _, error_type = _rag_failure_details(exc, "rag_query_unexpected_error")
            _LOGGER.warning(
                "RAG response unavailable: code=%s error_type=%s record_id=%d",
                code,
                error_type,
                record_id,
            )
            reply = _RAG_ERROR_REPLY
        else:
            _LOGGER.info(
                "RAG reply generation completed: record_id=%d duration_ms=%d",
                record_id,
                round((time.monotonic() - started_at) * 1000),
            )

        _LOGGER.info("WeChat reply send started: record_id=%d", record_id)
        try:
            sent = await asyncio.to_thread(_send_customer_message, to_user, reply)
        except Exception as exc:
            _LOGGER.warning(
                "WeChat reply failed: reason=worker_error error_type=%s record_id=%d",
                type(exc).__name__,
                record_id,
            )
            sent = False
        _set_message_result(record_id, reply, "accepted" if sent else "failed")
        _LOGGER.info(
            "WeChat reply task finished: status=%s record_id=%d duration_ms=%d",
            "accepted" if sent else "failed",
            record_id,
            round((time.monotonic() - started_at) * 1000),
        )
    except asyncio.CancelledError:
        _LOGGER.warning(
            "WeChat reply task cancelled: record_id=%d duration_ms=%d",
            record_id,
            round((time.monotonic() - started_at) * 1000),
        )
        raise



_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>公众号 · 馆藏 RAG</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #17211b; background: #f3f6f3; }
    body { max-width: 760px; margin: 0 auto; padding: 32px 18px 56px; }
    header, .notice, article { border: 1px solid #dce5de; border-radius: 14px; background: #fff; }
    header { padding: 24px; }
    .eyebrow { margin: 0; color: #16804a; font-size: .8rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
    h1 { margin: 8px 0; font-size: clamp(1.5rem, 5vw, 2rem); }
    header p:last-child { margin-bottom: 0; color: #58665d; }
    .notice { margin: 16px 0 24px; padding: 14px 16px; border-color: #ecd9a9; background: #fff9e9; color: #604b1b; line-height: 1.6; }
    .list-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
    h2 { font-size: 1.15rem; }
    #status { color: #64736a; font-size: .9rem; }
    #messages { display: grid; gap: 12px; }
    article { padding: 16px; overflow-wrap: anywhere; }
    time { color: #718078; font-size: .82rem; }
    .label { margin: 12px 0 4px; color: #56645b; font-size: .84rem; font-weight: 650; }
    .message-text, .reply-text { margin: 0; white-space: pre-wrap; line-height: 1.55; }
    .reply-status { margin: 0 0 8px; color: #56645b; font-size: .84rem; }
    .reply { margin-top: 14px; padding-top: 12px; border-top: 1px solid #e8eee9; }
    @media (max-width: 480px) { body { padding: 16px 12px 36px; } header { padding: 18px; } .list-heading { display: block; } }
  </style>
</head>
<body>
  <header>
    <p class="eyebrow">Official account · 馆藏 RAG</p>
    <h1>公众号智能问答</h1>
    <p>页面显示本地收到的文字、RAG 回答与馆藏来源；回调确认后，后台检索馆藏并调用配置的远程模型 API 生成回答，再尝试通过微信客服消息接口发送。</p>
</header>
  <section aria-labelledby="messages-title">
  <aside class="notice"><strong>远程 AI 处理：</strong>真实问题正文和命中的馆藏片段会发送到配置的模型 API；答案与拒答能力尚未系统评测。页面未做登录认证，仅供受信任的本机用户访问；不得将看板或 <code>/messages</code> 公开。最多保留最近 100 条，服务重启后清空。</aside>
    <div class="list-heading"><h2 id="messages-title">最近消息</h2><span id="status" role="status">正在连接本机服务…</span></div>
    <div id="messages" aria-live="polite"></div>
  </section>
  <script>
    const list = document.querySelector("#messages");
    const status = document.querySelector("#status");

    function render(messages) {
      if (messages.length === 0) {
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "尚未收到文字消息。发送消息后，本页面会自动更新。";
        list.replaceChildren(empty);
        return;
      }
      const cards = messages.map((message) => {
        const card = document.createElement("article");
        const time = document.createElement("time");
        time.dateTime = message.timestamp;
        time.textContent = new Date(message.timestamp).toLocaleString();
        const incomingLabel = document.createElement("p");
        incomingLabel.className = "label";
        incomingLabel.textContent = "收到的消息";
        const incomingText = document.createElement("p");
        incomingText.className = "message-text";
        incomingText.textContent = message.text;
        const replyBox = document.createElement("div");
        replyBox.className = "reply";
        const replyLabel = document.createElement("p");
        replyLabel.className = "label";
        replyLabel.textContent = "RAG 回答";
        const replyStatus = document.createElement("p");
        replyStatus.className = "reply-status";
        const replyStatusLabels = { pending: "正在检索并生成回答", accepted: "客服接口已接受", failed: "生成或发送失败" };
        replyStatus.textContent = `发送状态：${replyStatusLabels[message.reply_status] || "未知"}`;
        const replyText = document.createElement("p");
        replyText.className = "reply-text";
        replyText.textContent = message.reply;
        replyBox.append(replyLabel, replyStatus, replyText);
        card.append(time, incomingLabel, incomingText, replyBox);
        return card;
      });
      list.replaceChildren(...cards);
    }

    async function refresh() {
      try {
        const response = await fetch("/messages", { cache: "no-store" });
        if (!response.ok) throw new Error("message list unavailable");
        render(await response.json());
        status.textContent = "每 2 秒自动更新";
      } catch {
        status.textContent = "暂时无法连接本机服务";
      }
    }

    refresh();
    window.setInterval(refresh, 2000);
  </script>
</body>
</html>"""

def _verify_cloud_headers(request: Request) -> None:
    checks = (
        ("x-wx-source", _CLOUD_EXPECTED_SOURCE),
        ("x-wx-env", _CLOUD_EXPECTED_ENV),
        ("x-wx-appid", _CLOUD_EXPECTED_APPID),
    )
    for header_name, expected in checks:
        if not expected:
            continue
        actual = request.headers.get(header_name, "")
        if not actual or not hmac.compare_digest(actual, expected):
            _LOGGER.warning(
                "WeChat cloud callback rejected: reason=header_mismatch header=%s",
                header_name,
            )
            raise HTTPException(403, "invalid cloud push source")


def _verify_signature(request: Request) -> None:
    if not _TOKEN:
        raise HTTPException(503, "signature verification is unavailable")
    fields = [request.query_params.getlist(key) for key in ("timestamp", "nonce", "signature")]
    if any(len(values) != 1 or not values[0] for values in fields):
        raise HTTPException(403, "invalid signature")
    timestamp, nonce, signature = (values[0] for values in fields)
    expected = hashlib.sha1("".join(sorted((_TOKEN, timestamp, nonce))).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
        raise HTTPException(403, "invalid signature")
    if request.query_params.get("encrypt_type") not in (None, "raw"):
        raise HTTPException(400, "only plaintext messages are supported")


@dataclass(frozen=True)
class _ParsedCallback:
    msg_type: str
    content: str
    from_user: str
    message_id: str | None
    to_user: str = ""
    create_time: str = ""
    event: str = ""

    def summary_tuple(self) -> tuple[str, str, str, str | None]:
        return (self.msg_type, self.content, self.from_user, self.message_id)


def _scalar_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    return ""


def _parse_xml_callback_fields(body: bytes, *, allow_check: bool) -> _ParsedCallback | None:
    try:
        message = SafeET.fromstring(body.decode("utf-8"), forbid_dtd=True)
    except (UnicodeDecodeError, ET.ParseError, DefusedXmlException, ValueError):
        raise HTTPException(400, "invalid XML") from None
    if message.tag != "xml" or message.find("Encrypt") is not None:
        raise HTTPException(400, "only plaintext XML messages are supported")
    if allow_check and message.findtext("action") == "CheckContainerPath":
        return None
    return _ParsedCallback(
        msg_type=message.findtext("MsgType", ""),
        content=message.findtext("Content", ""),
        from_user=message.findtext("FromUserName", ""),
        message_id=message.findtext("MsgId"),
        to_user=message.findtext("ToUserName", ""),
        create_time=message.findtext("CreateTime", ""),
        event=message.findtext("Event", ""),
    )


def _parse_xml_callback(body: bytes) -> tuple[str, str, str, str | None]:
    parsed = _parse_xml_callback_fields(body, allow_check=False)
    assert parsed is not None
    return parsed.summary_tuple()


def _parse_cloud_callback_fields(body: bytes) -> _ParsedCallback | None:
    if body.lstrip().startswith(b"{"):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise HTTPException(400, "invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid JSON")
        if payload.get("action") == "CheckContainerPath":
            return None
        message_id = payload.get("MsgId")
        if message_id is not None and (type(message_id) is bool or not isinstance(message_id, (str, int))):
            raise HTTPException(400, "invalid message ID")
        return _ParsedCallback(
            msg_type=payload.get("MsgType") if isinstance(payload.get("MsgType"), str) else "",
            content=payload.get("Content") if isinstance(payload.get("Content"), str) else "",
            from_user=payload.get("FromUserName") if isinstance(payload.get("FromUserName"), str) else "",
            message_id=str(message_id) if message_id is not None else None,
            to_user=payload.get("ToUserName") if isinstance(payload.get("ToUserName"), str) else "",
            create_time=_scalar_text(payload.get("CreateTime")),
            event=payload.get("Event") if isinstance(payload.get("Event"), str) else "",
        )

    return _parse_xml_callback_fields(body, allow_check=True)


def _parse_cloud_callback(body: bytes) -> tuple[str, str, str, str | None] | None:
    parsed = _parse_cloud_callback_fields(body)
    return parsed.summary_tuple() if parsed is not None else None


_KNOWN_WECHAT_TYPES = {
    "text",
    "image",
    "voice",
    "video",
    "shortvideo",
    "location",
    "link",
    "event",
}


def _safe_msg_type(msg_type: str) -> str:
    return msg_type if msg_type in _KNOWN_WECHAT_TYPES else "unknown"


def _build_forward_envelope(
    *,
    raw_body: bytes,
    parsed: _ParsedCallback,
    cloud_push: bool,
    request: Request,
) -> dict[str, object]:
    wx_headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered.startswith("x-wx-"):
            wx_headers[lowered] = value[:512]
            if len(wx_headers) >= 32:
                break
    return {
        "schema_version": 1,
        "transport": "cloud" if cloud_push else "signed",
        "received_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "content_type": (request.headers.get("content-type") or "")[:128],
        "msg_type": parsed.msg_type,
        "msg_id": parsed.message_id,
        "from_user": parsed.from_user,
        "to_user": parsed.to_user,
        "create_time": parsed.create_time,
        "content": parsed.content,
        "event": parsed.event,
        "wx_headers": wx_headers,
        "raw_body": raw_body.decode("utf-8", errors="replace"),
    }


def _forward_callback(envelope: dict[str, object]) -> tuple[bool, dict[str, object] | None]:
    target_url = _validate_forward_url(_FORWARD_URL)
    if not target_url:
        return True, None
    safe_type = _safe_msg_type(str(envelope.get("msg_type", "")))
    transport = "cloud" if envelope.get("transport") == "cloud" else "signed"
    started_at = time.monotonic()
    _LOGGER.info("WeChat callback forward started: type=%s transport=%s", safe_type, transport)

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "WeChatRelay/1.0",
    }
    if _FORWARD_TOKEN:
        headers["Authorization"] = f"Bearer {_FORWARD_TOKEN}"
    data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = URLRequest(target_url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=_FORWARD_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            response_body = response.read()
    except HTTPError as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000)
        _LOGGER.warning(
            "WeChat callback forward failed: status=%d error_type=HTTPError duration_ms=%d",
            exc.code,
            duration_ms,
        )
        return False, None
    except URLError as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000)
        reason = exc.reason
        reason_type = type(reason).__name__ if reason is not None else "unknown"
        reason_errno = getattr(reason, "errno", None)
        _LOGGER.warning(
            "WeChat callback forward failed: status=none error_type=URLError reason_type=%s errno=%s duration_ms=%d",
            reason_type,
            reason_errno if type(reason_errno) is int else "-",
            duration_ms,
        )
        return False, None
    except Exception as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000)
        _LOGGER.warning(
            "WeChat callback forward failed: status=none error_type=%s duration_ms=%d",
            type(exc).__name__,
            duration_ms,
        )
        return False, None

    duration_ms = round((time.monotonic() - started_at) * 1000)
    if not (200 <= status < 300):
        _LOGGER.warning(
            "WeChat callback forward failed: status=%d error_type=UnexpectedStatus duration_ms=%d",
            status,
            duration_ms,
        )
        return False, None
    parsed_response: dict[str, object] | None = None
    if response_body:
        try:
            decoded = json.loads(response_body.decode("utf-8"))
            if isinstance(decoded, dict):
                parsed_response = decoded
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            parsed_response = None
    _LOGGER.info(
        "WeChat callback forward succeeded: status=%d duration_ms=%d",
        status,
        duration_ms,
    )
    return True, parsed_response


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    if not _DASHBOARD_ENABLED:
        raise HTTPException(404, "not found")
    return HTMLResponse(_DASHBOARD_HTML, headers=_HEADERS)


@app.get("/messages", response_class=JSONResponse)
async def recent_messages() -> JSONResponse:
    if not _DASHBOARD_ENABLED:
        raise HTTPException(404, "not found")
    return JSONResponse(_recent_messages(), headers=_HEADERS)


@app.get("/healthz", response_class=PlainTextResponse)
async def health() -> PlainTextResponse:
    return PlainTextResponse("ok", headers=_HEADERS)


@app.post("/debug/rag/query", response_class=JSONResponse)
async def debug_rag_query(request: Request) -> JSONResponse:
    if not _AUTO_REPLY_ENABLED or not _RAG_DEBUG_TOKEN:
        raise HTTPException(404, "not found")
    scheme, separator, supplied_token = request.headers.get("authorization", "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not supplied_token
        or not hmac.compare_digest(supplied_token, _RAG_DEBUG_TOKEN)
    ):
        raise HTTPException(
            401,
            "invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(422, "JSON object required")
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(422, "question must be a non-empty string")
    if len(question) > MAX_RAG_DEBUG_QUESTION_CHARS:
        raise HTTPException(413, "question is too long")

    responder = getattr(app.state, "rag_responder", None)
    if responder is None:
        raise HTTPException(503, "RAG responder is not ready")
    try:
        answer = await asyncio.to_thread(responder.answer, question)
    except Exception as exc:
        code, message, error_type = _rag_failure_details(
            exc, "rag_query_unexpected_error"
        )
        _LOGGER.warning(
            "RAG debug query failed: code=%s error_type=%s",
            code,
            error_type,
        )
        raise HTTPException(
            502,
            {"code": code, "message": message},
        ) from None
    return JSONResponse({"answer": answer}, headers=_HEADERS)


@app.get("/wechat", response_class=PlainTextResponse)
async def verify_callback(request: Request) -> PlainTextResponse:
    _verify_signature(request)
    echoes = request.query_params.getlist("echostr")
    if len(echoes) != 1:
        raise HTTPException(400, "exactly one echostr is required")
    return PlainTextResponse(echoes[0], headers=_HEADERS)


@app.post("/wechat")
async def receive_callback(
    request: Request, background_tasks: BackgroundTasks
) -> PlainTextResponse:
    cloud_push = _CLOUD_PUSH_ENABLED and bool(request.headers.get("x-wx-source"))
    if not cloud_push:
        _verify_signature(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "message exceeds 256 KiB")
        body.extend(chunk)

    raw_body = bytes(body)
    if cloud_push:
        parsed_fields = _parse_cloud_callback_fields(raw_body)
        if parsed_fields is None:
            return PlainTextResponse("success", headers=_HEADERS)
        _verify_cloud_headers(request)
    else:
        parsed_fields = _parse_xml_callback_fields(raw_body, allow_check=False)
        assert parsed_fields is not None
    msg_type, question, from_user, message_id = parsed_fields.summary_tuple()

    # Never log the URL, signature, OpenID, MsgId, or message content.
    _LOGGER.info(
        "WeChat callback accepted: type=%s transport=%s",
        _safe_msg_type(msg_type),
        "cloud" if cloud_push else "signed",
    )
    if _FORWARD_URL:
        envelope = _build_forward_envelope(
            raw_body=raw_body,
            parsed=parsed_fields,
            cloud_push=cloud_push,
            request=request,
        )
        forwarded, forward_response = await asyncio.to_thread(_forward_callback, envelope)
        if not forwarded and not _FORWARD_FAIL_OPEN:
            raise HTTPException(502, "callback forward failed")
        if (
            forwarded
            and _FORWARD_ALLOW_TEST_REPLY
            and isinstance(forward_response, dict)
            and not forward_response.get("duplicate")
            and from_user
        ):
            test_reply = forward_response.get("cloud_test_reply")
            if isinstance(test_reply, str) and test_reply.strip():
                sent = await asyncio.to_thread(_send_customer_message, from_user, test_reply)
                _LOGGER.info("WeChat forward test reply finished: sent=%s", str(sent).lower())
    if msg_type == "text":
        if not _AUTO_REPLY_ENABLED:
            return PlainTextResponse("success", headers=_HEADERS)
        responder = getattr(app.state, "rag_responder", None)
        if responder is None:
            _LOGGER.error("WeChat text callback rejected: reason=rag_not_ready")
            raise HTTPException(503, "RAG responder is not ready")
        record_id = _store_message(question, message_id)
        if record_id is None:
            _LOGGER.warning("WeChat reply task skipped: reason=duplicate_message")
        else:
            background_tasks.add_task(
                _answer_then_send,
                responder,
                question,
                from_user,
                record_id,
            )
            _LOGGER.info("WeChat reply task scheduled: record_id=%d", record_id)
    return PlainTextResponse("success", headers=_HEADERS)
