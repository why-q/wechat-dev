"""Server-rendered librarian and administrator dashboards for the support store."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hmac
import html
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .db import SupportStore
from .dashboard_inbox import render_inbox


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "strict-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Vary": "Authorization",
}
_MAX_FORM_BYTES = 4096
_MAX_IDENTIFIER_LENGTH = 255
_MAX_SQLITE_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class _DashboardSettings:
    public_origin: str | None
    admin_password: str
    librarian_password: str

    @property
    def enabled(self) -> bool:
        return bool(self.public_origin and (self.admin_password or self.librarian_password))


def _format_origin(host: str, port: int | None) -> str:
    normalized_host = host.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    port_suffix = f":{port}" if port is not None and port != 443 else ""
    return f"https://{normalized_host}{port_suffix}"


def _normalize_https_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return _format_origin(parsed.hostname, port)


def _origin_from_host(value: str) -> str | None:
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return _format_origin(parsed.hostname, port)


def _secure_public_request(request: Request, settings: _DashboardSettings) -> bool:
    return bool(
        settings.enabled
        and settings.public_origin
        and request.url.scheme.lower() == "https"
        and _origin_from_host(request.headers.get("host", "")) == settings.public_origin
    )


def _plain_error(status_code: int, detail: str, *, challenge: str = "") -> PlainTextResponse:
    headers = dict(_SECURITY_HEADERS)
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return PlainTextResponse(detail, status_code=status_code, headers=headers)


def _authenticate(
    request: Request,
    settings: _DashboardSettings,
    *,
    allowed_roles: frozenset[str],
) -> tuple[str | None, Response | None]:
    if not settings.enabled or not any(
        password for role, password in (
            ("admin", settings.admin_password),
            ("librarian", settings.librarian_password),
        ) if role in allowed_roles
    ):
        return None, _plain_error(404, "not found")
    if not _secure_public_request(request, settings):
        return None, _plain_error(404, "not found")

    realm = "Library support administrator" if allowed_roles == frozenset({"admin"}) else "Library support librarian"
    challenge = f'Basic realm="{realm}", charset="UTF-8"'
    authorization = request.headers.get("authorization", "")
    scheme, separator, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not separator or not encoded or len(encoded) > 4096:
        return None, _plain_error(401, "authentication required", challenge=challenge)
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, _plain_error(401, "invalid credentials", challenge=challenge)
    username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return None, _plain_error(401, "invalid credentials", challenge=challenge)

    configured_passwords = {
        "admin": settings.admin_password,
        "librarian": settings.librarian_password,
    }
    expected_password = configured_passwords.get(username)
    candidate_bytes = supplied_password.encode("utf-8")
    expected_bytes = expected_password.encode("utf-8") if expected_password else b"dashboard-disabled"
    password_matches = hmac.compare_digest(candidate_bytes, expected_bytes)
    if not expected_password or not password_matches:
        return None, _plain_error(401, "invalid credentials", challenge=challenge)
    if username not in allowed_roles:
        return None, _plain_error(403, "administrator access required")
    return username, None


def _store_from_request(request: Request) -> SupportStore | None:
    pipeline = getattr(request.app.state, "support_pipeline", None)
    store = getattr(pipeline, "store", None)
    return store if isinstance(store, SupportStore) else None


def _response(body: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=_SECURITY_HEADERS)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _count(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError, OverflowError):
        return "0"


def _metric(label: str, value: object, note: str) -> str:
    return (
        '<article class="metric">'
        f'<h2 class="metric-label">{_escape(label)}</h2>'
        f'<p class="metric-value">{_count(value)}</p>'
        f'<p class="metric-note">{_escape(note)}</p>'
        "</article>"
    )


def _reply_mode(pipeline: object) -> str:
    if not bool(getattr(pipeline, "auto_reply_enabled", False)):
        return "自动回复已关闭；自动回复只记录为影子状态，不调用发送接口。"
    if getattr(pipeline, "send_text_fn", None) is None:
        return "自动回复开关已开启，但发送客户端不可用；回复只记录为影子状态。"
    if getattr(pipeline, "auto_reply_allowlist", ()) or getattr(pipeline, "gray_enroll_keyword", ""):
        return "灰度发送已开启：静态或动态允许名单中的读者可能收到真实回复，其余仍记录为影子状态。"
    return "自动回复已开启且没有白名单门控；符合处理流程的读者可能收到真实回复。"


def _status_banner(text: str) -> str:
    return f'<p class="status-note" role="status">{_escape(text)}</p>'


def _page_frame(*, role: str, active: str, main: str, footer: str) -> str:
    nav_items = [("librarian", "/dashboard/librarian", "普通馆员")]
    if role == "admin":
        nav_items.insert(0, ("admin", "/dashboard/admin", "管理员"))
    links = []
    for name, href, label in nav_items:
        current = ' aria-current="page"' if active == name else ""
        links.append(f'<a href="{href}"{current}>{label}</a>')
    navigation = "".join(links)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="referrer" content="strict-origin">
  <title>图书馆智能客服 · {_escape(footer)}</title>
  <link rel="stylesheet" href="/dashboard/static/dashboard.css">
</head>
<body>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="shell">
    <header class="masthead">
      <a class="brand" href="/dashboard/librarian" aria-label="图书馆智能客服，工作台">
        <span class="brand-mark" aria-hidden="true">馆</span>
        <span class="brand-name">图书馆智能客服</span>
      </a>
      <nav class="role-switch" aria-label="工作视图">{navigation}</nav>
    </header>
    <main id="main-content">{main}</main>
    <footer class="footer"><span>{_escape(footer)}</span><span>仅授权馆员访问 · 页面不缓存</span></footer>
  </div>
</body>
</html>"""


def _page_head(*, eyebrow: str, title: str, description: str, meta: str) -> str:
    return (
        '<section class="page-head" aria-labelledby="page-title"><div>'
        f'<p class="eyebrow">{_escape(eyebrow)}</p>'
        f'<h1 id="page-title">{_escape(title)}</h1>'
        f'<p class="lede">{_escape(description)}</p>'
        f'</div><div class="page-meta"><span class="demo-tag">{_escape(meta)}</span></div></section>'
    )


_CATEGORY_LABELS = {
    "question": "普通问答",
    "feedback": "意见反馈",
    "emergency": "紧急事项",
    "staff_action": "人工跟进",
    "gray_enroll": "白名单入组",
    "invalid": "无效消息",
    "unsupported": "不支持类型",
}
_REPLY_LABELS = {
    "sent": ("微信接口已受理", "不代表读者已读"),
    "shadow": ("影子模式", "仅记录，未调用发送接口"),
    "failed": ("发送失败", "接口或传输失败"),
}


def _data_table(title: str, headers: tuple[str, str], rows: list[tuple[str, object, str]]) -> str:
    body = "".join(
        '<tr>'
        f'<th scope="row">{_escape(label)}<span class="row-note">{_escape(note)}</span></th>'
        f'<td class="number-cell">{_count(value)}</td>'
        "</tr>"
        for label, value, note in rows
    )
    if not body:
        body = '<tr><td colspan="2">暂无记录</td></tr>'
    return (
        '<div class="table-wrap"><table class="data-table">'
        f'<caption class="sr-only">{_escape(title)}</caption>'
        f'<thead><tr><th scope="col">{_escape(headers[0])}</th>'
        f'<th scope="col" class="number-cell">{_escape(headers[1])}</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


def _overview_panels(metrics: dict[str, object]) -> str:
    raw_categories = metrics.get("inbound_categories", {})
    categories = raw_categories if isinstance(raw_categories, dict) else {}
    category_rows = [
        (_CATEGORY_LABELS.get(str(key), str(key)), value, "Filter 分类累计")
        for key, value in sorted(categories.items(), key=lambda item: str(item[0]))
    ]

    raw_replies = metrics.get("outbound_replies", {})
    replies = raw_replies if isinstance(raw_replies, dict) else {}
    reply_rows = []
    for key, value in sorted(replies.items(), key=lambda item: str(item[0])):
        status = str(key)
        label, note = _REPLY_LABELS.get(status, (status, "实际记录状态"))
        reply_rows.append((label, value, note))

    batch_rows = [
        ("待处理", metrics.get("open_batches", 0), "open"),
        ("处理中", metrics.get("processing_batches", 0), "processing"),
        ("已排队", metrics.get("queued_batches", 0), "queued"),
        ("已冲刷", metrics.get("flushed_batches", 0), "flushed"),
    ]
    return (
        '<div class="dashboard-grid" aria-label="服务运营详情">'
        '<section class="panel panel--traffic" aria-labelledby="category-title">'
        '<div class="panel-head"><div><p class="eyebrow">入站记录</p>'
        '<h2 id="category-title">Filter 分类</h2>'
        '<p class="panel-note">按实际入库记录累计</p></div></div>'
        f'{_data_table("Filter 分类累计", ("类别", "记录数"), category_rows)}'
        "</section>"
        '<section class="panel panel--outcomes" aria-labelledby="reply-title">'
        '<div class="panel-head"><div><p class="eyebrow">出站记录</p>'
        '<h2 id="reply-title">回复状态</h2>'
        '<p class="panel-note">实际状态计数，不代表读者已读</p></div></div>'
        f'{_data_table("回复状态", ("状态", "记录数"), reply_rows)}'
        "</section>"
        '<section class="panel panel--operations" aria-labelledby="batch-title">'
        '<div class="panel-head"><div><p class="eyebrow">队列</p>'
        '<h2 id="batch-title">问答批次</h2>'
        '<p class="panel-note">按 SQLite 中当前批次状态统计</p></div></div>'
        f'{_data_table("问答批次状态", ("批次状态", "批次数"), batch_rows)}'
        "</section></div>"
    )




def _dynamic_allowlist_rows(entries: list[dict[str, object]]) -> str:
    rows: list[str] = []
    for entry in entries:
        account_id = str(entry.get("account_id", ""))
        reader_id = str(entry.get("reader_id", ""))
        source = str(entry.get("source", ""))
        updated = str(entry.get("updated_at_utc", ""))
        rows.append(
            "<tr>"
            f'<td><code>{_escape(account_id)}</code></td>'
            f'<td class="allowlist-reader"><code>{_escape(reader_id)}</code></td>'
            f'<td>{_escape(source)}</td>'
            f'<td>{_escape(updated)}</td>'
            '<td><form class="allowlist-remove" method="post" action="/dashboard/admin/allowlist/remove">'
            f'<input type="hidden" name="account_id" value="{_escape(account_id)}">'
            f'<input type="hidden" name="reader_id" value="{_escape(reader_id)}">'
            '<label class="confirm-label"><input type="checkbox" name="confirm" value="yes" required> 确认</label>'
            '<button class="button button--quiet" type="submit">移出</button>'
            "</form></td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="5">动态名单暂无条目。</td></tr>'


def _admin_allowlist_panel(store: SupportStore, pipeline: object, metrics: dict[str, object]) -> str:
    entries = store.list_allowlist(limit=200)
    dynamic_total = int(metrics.get("dynamic_allowlist_count", 0))
    static_total = len(getattr(pipeline, "auto_reply_allowlist", ()))
    table = (
        '<div class="table-wrap"><table class="data-table allowlist-table">'
        '<caption class="sr-only">动态自动回复允许名单</caption>'
        '<thead><tr><th scope="col">账户 ID</th><th scope="col">读者 OpenID</th>'
        '<th scope="col">来源</th><th scope="col">最近更新</th><th scope="col">操作</th></tr></thead>'
        f'<tbody>{_dynamic_allowlist_rows(entries)}</tbody></table></div>'
    )
    return (
        '<div class="dashboard-grid"><section class="panel panel--allowlist" aria-labelledby="allowlist-title">'
        '<div class="panel-head"><div><p class="eyebrow">管理员专属</p>'
        '<h2 id="allowlist-title">动态自动回复名单</h2>'
        f'<p class="panel-note">动态条目 {_count(dynamic_total)} 条，环境变量静态名单 {_count(static_total)} 条，只读。</p>'
        '</div></div>'
        '<p class="allowlist-explainer">添加或移除动态名单会影响后续回复。仅当自动回复与白名单门控开启时，名单成员才可能收到实发；关闭自动回复时仍为影子记录。静态环境变量名单不会在此页面修改。</p>'
        '<form class="allowlist-add-form" method="post" action="/dashboard/admin/allowlist/add">'
        '<label>账户 ID<input class="form-control" name="account_id" value="default" maxlength="255" required autocomplete="off"></label>'
        '<label>读者 OpenID<input class="form-control" name="reader_id" maxlength="255" required autocomplete="off"></label>'
        '<button class="button" type="submit">加入动态名单</button>'
        '</form>'
        f'<h3 class="subsection-title">最近更新的动态条目 <span>展示最多 200 条，旧条目可按账户 ID 与 OpenID 指定移除</span></h3>{table}'
        '<form class="allowlist-remove-known" method="post" action="/dashboard/admin/allowlist/remove">'
        '<h3 class="subsection-title">移除列表外条目</h3>'
        '<label>账户 ID<input class="form-control" name="account_id" value="default" maxlength="255" required autocomplete="off"></label>'
        '<label>读者 OpenID<input class="form-control" name="reader_id" maxlength="255" required autocomplete="off"></label>'
        '<label class="confirm-label"><input type="checkbox" name="confirm" value="yes" required> 确认只移除该账户下该读者</label>'
        '<button class="button button--quiet" type="submit">移除指定条目</button>'
        '</form>'
        '<p class="panel-footnote">不会清空名单，也不会修改静态环境变量配置。</p>'
        '</section></div>'
    )


_RESULT_NOTICES = {
    "added": "已加入动态名单。",
    "existing": "条目已存在，已更新来源。",
    "removed": "已移出动态名单。",
    "not-found": "没有找到该动态条目。",
    "invalid": "操作未执行，请检查输入。",
}


def _admin_dashboard(pipeline: object, store: SupportStore, *, result: str = "") -> str:
    metrics = store.overview_metrics()
    categories = metrics.get("inbound_categories", {})
    inbound_total = sum(int(value) for value in categories.values()) if isinstance(categories, dict) else 0
    open_batches = sum(
        int(metrics.get(key, 0))
        for key in ("open_batches", "processing_batches", "queued_batches")
    )
    replies = metrics.get("outbound_replies", {})
    reply_total = sum(int(value) for value in replies.values()) if isinstance(replies, dict) else 0
    notice = _RESULT_NOTICES.get(result, "")
    main = (
        _page_head(
            eyebrow="运营台 / 管理员",
            title="服务总览",
            description="查看实际入站分类、当前问答队列、馆员待办与回复状态。",
            meta="实时存储数据",
        )
        + '<section class="metric-strip" aria-label="关键指标">'
        + _metric("累计收到消息", inbound_total, "Filter 入站记录")
        + _metric("未结问答批次", open_batches, "待处理、处理中与已排队")
        + _metric("未结馆员待办", metrics.get("open_staff_cases", 0), "staff_cases 状态为 open")
        + _metric("回复记录", reply_total, "按实际出站状态分类")
        + "</section>"
        + _status_banner(_reply_mode(pipeline))
        + (f'<p class="form-notice" role="status">{_escape(notice)}</p>' if notice else "")
        + _overview_panels(metrics)
        + _admin_allowlist_panel(store, pipeline, metrics)
    )
    return _page_frame(role="admin", active="admin", main=main, footer="管理员工作视图")


def _valid_identifier(value: str) -> bool:
    return bool(value and len(value) <= _MAX_IDENTIFIER_LENGTH and all(character.isprintable() for character in value))


async def _read_form(request: Request) -> dict[str, str] | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BYTES:
            return None
        body.extend(chunk)
    try:
        raw = body.decode("utf-8")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    except (UnicodeDecodeError, ValueError):
        return None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            return None
        values[key] = value
    return values


def _csrf_valid(request: Request, settings: _DashboardSettings) -> bool:
    origin = _normalize_https_origin(request.headers.get("origin", ""))
    if origin != settings.public_origin:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is None or fetch_site == "same-origin"


def _redirect_result(request: Request, result: str) -> RedirectResponse:
    return RedirectResponse(
        f"/dashboard/admin?result={result}",
        status_code=303,
        headers=_SECURITY_HEADERS,
    )


def create_dashboard_router(
    *,
    admin_password: str = "",
    librarian_password: str = "",
    public_origin: str = "",
) -> APIRouter:
    """Create routes with independent Basic credentials and a fixed HTTPS origin."""
    settings = _DashboardSettings(
        public_origin=_normalize_https_origin(public_origin),
        admin_password=admin_password,
        librarian_password=librarian_password,
    )
    router = APIRouter()

    @router.get("/dashboard/static/dashboard.css")
    async def dashboard_stylesheet(request: Request) -> Response:
        if not _secure_public_request(request, settings):
            return _plain_error(404, "not found")
        package_css = Path(__file__).with_name("dashboard.css")
        fallback_css = Path(__file__).resolve().parents[1] / "docs" / "dashboard-demo" / "dashboard.css"
        stylesheet = package_css if package_css.is_file() else fallback_css
        if not stylesheet.is_file():
            return _plain_error(404, "not found")
        return FileResponse(stylesheet, media_type="text/css", headers=_SECURITY_HEADERS)

    @router.get("/dashboard/librarian")
    async def librarian_dashboard(request: Request) -> Response:
        role, error = _authenticate(
            request,
            settings,
            allowed_roles=frozenset({"admin", "librarian"}),
        )
        if error is not None:
            return error
        pipeline = getattr(request.app.state, "support_pipeline", None)
        store = _store_from_request(request)
        if pipeline is None or store is None:
            return _plain_error(503, "dashboard data unavailable")

        before_values = request.query_params.getlist("before")
        if len(before_values) > 1:
            return _plain_error(400, "invalid inbox cursor")
        before = None
        if before_values:
            try:
                before = int(before_values[0])
            except ValueError:
                return _plain_error(400, "invalid inbox cursor")
            if before < 1 or before > _MAX_SQLITE_INTEGER:
                return _plain_error(400, "invalid inbox cursor")
        inbox = render_inbox(store, before=before)
        main = (
            _page_head(
                eyebrow="工作台 / 普通馆员",
                title="读者私信",
                description="影子审核期间，读者私信持续显示为未结；回复状态不会自动结案。",
                meta="实时存储数据",
            )
            + _status_banner(_reply_mode(pipeline))
            + inbox
        )
        return _response(_page_frame(role=role or "librarian", active="librarian", main=main, footer="馆员工作视图"))

    @router.get("/dashboard/admin")
    async def admin_dashboard(request: Request) -> Response:
        _, error = _authenticate(request, settings, allowed_roles=frozenset({"admin"}))
        if error is not None:
            return error
        pipeline = getattr(request.app.state, "support_pipeline", None)
        store = _store_from_request(request)
        if pipeline is None or store is None:
            return _plain_error(503, "dashboard data unavailable")
        return _response(
            _admin_dashboard(
                pipeline,
                store,
                result=request.query_params.get("result", ""),
            )
        )

    async def mutate_allowlist(request: Request, *, operation: str) -> Response:
        _, error = _authenticate(request, settings, allowed_roles=frozenset({"admin"}))
        if error is not None:
            return error
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        account_id = form.get("account_id", "").strip()
        reader_id = form.get("reader_id", "").strip()
        if not _valid_identifier(account_id) or not _valid_identifier(reader_id):
            return _redirect_result(request, "invalid")
        if operation == "remove":
            if form.get("confirm") != "yes":
                return _redirect_result(request, "invalid")
            removed = store.remove_allowlist(reader_id, account_id=account_id)
            return _redirect_result(request, "removed" if removed else "not-found")
        added = store.enroll_allowlist(
            account_id=account_id,
            reader_id=reader_id,
            source="admin",
        )
        return _redirect_result(request, "added" if added else "existing")

    @router.post("/dashboard/admin/allowlist/add")
    async def add_allowlist(request: Request) -> Response:
        return await mutate_allowlist(request, operation="add")

    @router.post("/dashboard/admin/allowlist/remove")
    async def remove_allowlist(request: Request) -> Response:
        return await mutate_allowlist(request, operation="remove")

    return router


router = create_dashboard_router(
    admin_password=os.environ.get("SUPPORT_DASHBOARD_ADMIN_PASSWORD", ""),
    librarian_password=os.environ.get("SUPPORT_DASHBOARD_LIBRARIAN_PASSWORD", ""),
    public_origin=os.environ.get("SUPPORT_DASHBOARD_PUBLIC_ORIGIN", ""),
)
