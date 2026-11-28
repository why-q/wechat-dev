"""Server-rendered RBAC librarian, administrator, and preset-review dashboards."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import hmac
import html
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .auth import (
    ALL_LIBRARIAN_PERMISSIONS,
    DEFAULT_LIBRARIAN_PERMISSIONS,
    PERM_CLOSE_CASE,
    PERM_DRAFT_PRESET,
    PERM_REPLY_READER,
    PERM_REVIEW_PRESET,
    PERM_VIEW_WORKBENCH,
    DashboardPrincipal,
    normalize_permissions,
    valid_username,
)
from .dashboard_charts import (
    render_admin_overview,
    render_donut,
    render_grouped_pace_chart,
    render_micro_bar,
    render_sparkline,
    render_stack_hbar_list,
)
from .dashboard_inbox import render_inbox
from .dashboard_librarian import render_librarian_workbench
from .dashboard_pages import (
    render_admin_users_page,
    render_login_page,
    render_presets_review_page,
)
from .db import SupportStore
from .preset_workflow import (
    audit_preset_for_publish,
    evaluate_and_generate_preset_proposal,
    generate_preset_proposal,
)
from .presets import PresetCatalog, write_dynamic_preset

_SESSION_COOKIE_NAME = "library_dashboard_session"
_SESSION_TTL_SECONDS = 12 * 3600
_MAX_REPLY_UTF8_BYTES = 1800

_ADMIN_ALLOWLIST_SCRIPT = (
    "(function(){"
    "var a=document.getElementById('allowlist-select-all');"
    "var b=[].slice.call(document.querySelectorAll('.allowlist-check'));"
    "var c=document.getElementById('selected-count');"
    "var d=document.getElementById('batch-remove-details');"
    "if(!a||!b.length)return;"
    "function s(){"
    "var n=b.filter(function(x){return x.checked;}).length;"
    "a.checked=n===b.length;"
    "a.indeterminate=n>0&&n<b.length;"
    "if(c)c.textContent=n?' ('+n+')':'';"
    "if(d&&!n)d.removeAttribute('open');"
    "}"
    "a.addEventListener('change',function(){b.forEach(function(x){x.checked=a.checked;});s();});"
    "b.forEach(function(x){x.addEventListener('change',s);});"
    "s();"
    "})();"
)
_ADMIN_SCRIPT_SHA256 = base64.b64encode(
    hashlib.sha256(_ADMIN_ALLOWLIST_SCRIPT.encode("utf-8")).digest()
).decode("ascii")
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; script-src 'none'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "strict-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Vary": "Authorization, Cookie",
}
_ADMIN_SECURITY_HEADERS = {
    **_SECURITY_HEADERS,
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data:; script-src 'sha256-{_ADMIN_SCRIPT_SHA256}'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}
_TRASH_ICON_SVG = (
    '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M2.5 4h11"/>'
    '<path d="M6 4V2.5h4V4"/>'
    '<path d="M4 4l.7 9.2a1 1 0 0 0 1 .8h4.6a1 1 0 0 0 1-.8L12 4"/>'
    '<path d="M6.6 7v4.2M9.4 7v4.2"/>'
    "</svg>"
)
_FAVICON_DATA_URI = (
    "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E"
    "%3Crect width=%2732%27 height=%2732%27 rx=%275%27 fill=%27%231e4334%27/%3E"
    "%3Ctext x=%2716%27 y=%2722%27 text-anchor=%27middle%27 font-size=%2718%27 "
    "font-family=%27sans-serif%27 fill=%27%23fcfbf7%27%3E%E9%A6%86%3C/text%3E%3C/svg%3E"
)
_MAX_FORM_BYTES = 4096
_MAX_RICH_FORM_BYTES = 65536
_MAX_IDENTIFIER_LENGTH = 255
_MAX_NOTE_LENGTH = 500
_MAX_SQLITE_INTEGER = 2**63 - 1
_VALID_LIBRARIAN_VIEWS = frozenset({"all", "cases", "inbox", "feedback"})
_VALID_PRESET_TABS = frozenset({"draft", "published", "rejected", "active"})
_COLOR_CLASS_TO_STROKE = {
    "c-green": "var(--green)",
    "c-teal": "var(--teal)",
    "c-ochre": "var(--ochre)",
    "c-rust": "var(--rust)",
    "c-ink": "var(--ink)",
    "c-slate": "var(--slate)",
    "c-muted": "oklch(0.76 0.015 110)",
}

PresetLlmFn = Callable[[str, str], dict[str, Any]]


@dataclass(frozen=True)
class _DashboardSettings:
    public_origin: str | None
    admin_password: str
    librarian_password: str

    @property
    def enabled(self) -> bool:
        return bool(self.public_origin and (self.admin_password or self.librarian_password))


def _find_stylesheet_path() -> Path | None:
    root = Path(__file__).resolve().parents[1]
    candidates = (
        root / "dashboard.css",
        root / "docs" / "dashboard-demo" / "dashboard.css",
        Path(__file__).with_name("dashboard.css"),
    )
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _minify_css(css_text: str) -> str:
    stripped = re.sub(r"@charset\s+[^;]+;\s*", "", css_text)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"\s+", " ", stripped)
    stripped = re.sub(r"\s*([{}:;,>~])\s*", r"\1", stripped)
    stripped = stripped.replace(";}", "}")
    return stripped.strip()


@lru_cache(maxsize=4)
def _load_inline_css_cached(path_str: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    raw = Path(path_str).read_text(encoding="utf-8")
    return _minify_css(raw)


def _inline_css_tag() -> str:
    stylesheet = _find_stylesheet_path()
    if stylesheet is None:
        return '<link rel="stylesheet" href="/dashboard/static/dashboard.css">'
    try:
        stat = stylesheet.stat()
        minified = _load_inline_css_cached(str(stylesheet), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return '<link rel="stylesheet" href="/dashboard/static/dashboard.css">'
    if not minified:
        return '<link rel="stylesheet" href="/dashboard/static/dashboard.css">'
    return f"<style>{minified}</style>"


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


def _safe_next_path(value: str, *, default: str = "/dashboard/librarian") -> str:
    raw = (value or "").strip()
    if raw.startswith("/dashboard/") and not raw.startswith("//") and "\r" not in raw and "\n" not in raw:
        return raw
    return default


def _plain_error(status_code: int, detail: str, *, challenge: str = "") -> PlainTextResponse:
    headers = dict(_SECURITY_HEADERS)
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return PlainTextResponse(detail, status_code=status_code, headers=headers)


def _store_from_request(request: Request) -> SupportStore | None:
    pipeline = getattr(request.app.state, "support_pipeline", None)
    store = getattr(pipeline, "store", None)
    return store if isinstance(store, SupportStore) else None


def _responder_from_request(request: Request) -> Any:
    return getattr(request.app.state, "support_responder", None)


def _verify_credentials(
    username: str,
    password: str,
    *,
    settings: _DashboardSettings,
    store: SupportStore | None,
) -> DashboardPrincipal | None:
    clean_user = username.strip()
    if not clean_user or not password:
        return None
    if store is not None:
        db_principal = store.verify_dashboard_user(clean_user, password)
        if db_principal is not None:
            return db_principal
        existing = store.get_dashboard_user(clean_user)
        if existing is not None:
            # DB account exists (disabled or wrong password); only allow env bootstrap
            # recovery for the fixed `admin` super-admin if env admin_password matches.
            if clean_user != "admin":
                return None

    env_passwords = {
        "admin": settings.admin_password,
        "librarian": settings.librarian_password,
    }
    expected = env_passwords.get(clean_user, "")
    candidate_bytes = password.encode("utf-8")
    expected_bytes = expected.encode("utf-8") if expected else b"dashboard-disabled"
    if expected and hmac.compare_digest(candidate_bytes, expected_bytes):
        if clean_user == "admin":
            return DashboardPrincipal(
                username="admin",
                display_name="超级管理员",
                role="admin",
                permissions=frozenset(ALL_LIBRARIAN_PERMISSIONS),
                source="env_admin",
            )
        return DashboardPrincipal(
            username="librarian",
            display_name="值班馆员",
            role="librarian",
            permissions=frozenset(DEFAULT_LIBRARIAN_PERMISSIONS),
            source="env_librarian",
        )
    return None


def _principal_from_session(
    request: Request,
    *,
    settings: _DashboardSettings,
    store: SupportStore | None,
) -> DashboardPrincipal | None:
    if store is None:
        return None
    token = (request.cookies.get(_SESSION_COOKIE_NAME) or "").strip()
    if not token or len(token) > 256:
        return None
    sess = store.get_dashboard_session(token)
    if sess is None:
        return None
    username = str(sess.get("username") or "").strip()
    sess_role = str(sess.get("role") or "").strip()
    if not username:
        return None
    db_user = store.get_dashboard_user(username)
    if db_user is not None:
        if not db_user.get("is_active"):
            store.delete_dashboard_session(token)
            return None
        role = str(db_user.get("role") or "librarian")
        perms = (
            frozenset(ALL_LIBRARIAN_PERMISSIONS)
            if role == "admin"
            else frozenset(normalize_permissions(db_user.get("permissions") or ()))
        )
        return DashboardPrincipal(
            username=username,
            display_name=str(db_user.get("display_name") or username),
            role=role,
            permissions=perms,
            source="db",
        )
    if username == "admin" and sess_role == "admin" and settings.admin_password:
        return DashboardPrincipal(
            username="admin",
            display_name="超级管理员",
            role="admin",
            permissions=frozenset(ALL_LIBRARIAN_PERMISSIONS),
            source="env_admin",
        )
    if username == "librarian" and sess_role == "librarian" and settings.librarian_password:
        return DashboardPrincipal(
            username="librarian",
            display_name="值班馆员",
            role="librarian",
            permissions=frozenset(DEFAULT_LIBRARIAN_PERMISSIONS),
            source="env_librarian",
        )
    return None


def _wants_html_login_redirect(request: Request) -> bool:
    if request.method.upper() != "GET":
        return False
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept


def _authenticate_principal(
    request: Request,
    settings: _DashboardSettings,
    *,
    require_admin: bool = False,
    required_permission: str | None = None,
    any_permissions: tuple[str, ...] = (),
) -> tuple[DashboardPrincipal | None, Response | None]:
    if not settings.enabled:
        return None, _plain_error(404, "not found")
    if require_admin and not settings.admin_password:
        store = _store_from_request(request)
        has_db_admin = bool(
            store is not None
            and any(u.get("role") == "admin" and u.get("is_active") for u in store.list_dashboard_users())
        )
        if not has_db_admin:
            return None, _plain_error(404, "not found")
    if not _secure_public_request(request, settings):
        return None, _plain_error(404, "not found")

    store = _store_from_request(request)
    realm = "Library support administrator" if require_admin else "Library support librarian"
    challenge = f'Basic realm="{realm}", charset="UTF-8"'
    principal: DashboardPrincipal | None = None

    authorization = request.headers.get("authorization", "")
    if authorization:
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
        principal = _verify_credentials(username, supplied_password, settings=settings, store=store)
        if principal is None:
            return None, _plain_error(401, "invalid credentials", challenge=challenge)
    else:
        principal = _principal_from_session(request, settings=settings, store=store)

    if principal is None:
        if _wants_html_login_redirect(request):
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            login_url = f"/dashboard/login?next={quote(_safe_next_path(target), safe='/?=&')}"
            return None, RedirectResponse(login_url, status_code=303, headers=_SECURITY_HEADERS)
        return None, _plain_error(401, "authentication required", challenge=challenge)

    if require_admin and not principal.is_admin:
        return None, _plain_error(403, "administrator access required")
    if required_permission and not principal.has_permission(required_permission):
        return None, _plain_error(403, "permission denied")
    if any_permissions and not any(principal.has_permission(p) for p in any_permissions):
        return None, _plain_error(403, "permission denied")
    return principal, None


def _response(body: str, *, status_code: int = 200, headers: dict[str, str] | None = None) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=headers or _SECURITY_HEADERS)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _count(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError, OverflowError):
        return "0"


def _reply_mode(pipeline: object) -> str:
    if not bool(getattr(pipeline, "auto_reply_enabled", False)):
        return "自动回复已关闭；自动回复只记录为影子状态，不调用发送接口（馆员工作台手工回复不受此限制）。"
    if getattr(pipeline, "send_text_fn", None) is None:
        return "自动回复开关已开启，但发送客户端不可用；回复只记录为影子状态。"
    if getattr(pipeline, "auto_reply_allowlist", ()) or getattr(pipeline, "gray_enroll_keyword", ""):
        return "灰度发送已开启：允许名单中的读者会收到自动回复，其余为影子记录；馆员在工作台可主动直连回复。"
    return "自动回复已开启且没有白名单门控；符合处理流程的读者可能收到真实回复。"


def _status_banner(text: str) -> str:
    return f'<p class="status-note" role="status">{_escape(text)}</p>'


def _page_frame(
    *,
    principal: DashboardPrincipal,
    active: str,
    main: str,
    footer: str,
    script: str = "",
) -> str:
    nav_items: list[tuple[str, str, str]] = []
    if principal.is_admin:
        nav_items.append(("admin", "/dashboard/admin", "服务总览"))
    if principal.has_permission(PERM_VIEW_WORKBENCH):
        nav_items.append(("librarian", "/dashboard/librarian", "馆员工作台"))
    if principal.has_permission(PERM_REVIEW_PRESET) or principal.has_permission(PERM_DRAFT_PRESET):
        nav_items.append(("presets", "/dashboard/presets", "标准模板"))
    if principal.is_admin:
        nav_items.append(("users", "/dashboard/admin/users", "账号权限"))

    links = []
    for name, href, label in nav_items:
        current = ' aria-current="page"' if active == name else ""
        links.append(f'<a href="{href}"{current}>{label}</a>')
    navigation = "".join(links)
    home_href = "/dashboard/admin" if principal.is_admin else "/dashboard/librarian"
    style_tag = _inline_css_tag()
    script_tag = f"<script>{script}</script>" if script else ""
    user_label = principal.display_name or principal.username
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="theme-color" content="#f3f1e9">
  <meta name="referrer" content="strict-origin">
  <title>图书馆智能客服 · {_escape(footer)}</title>
  <link rel="icon" href="{_FAVICON_DATA_URI}">
  {style_tag}
</head>
<body>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="shell">
    <header class="masthead">
      <a class="brand" href="{home_href}" aria-label="图书馆智能客服，工作台">
        <span class="brand-mark" aria-hidden="true">馆</span>
        <span class="brand-name">图书馆智能客服</span>
      </a>
      <div class="masthead-right">
        <nav class="role-switch" aria-label="工作视图">{navigation}</nav>
        <div class="nav-user">
          <span class="nav-user-name" title="{_escape(principal.username)}">{_escape(user_label)}</span>
          <form method="post" action="/dashboard/logout">
            <button class="nav-logout" type="submit">退出</button>
          </form>
        </div>
      </div>
    </header>
    <main id="main-content">{main}</main>
    <footer class="footer"><span>{_escape(footer)}</span><span>仅授权馆员访问 · 页面不缓存</span></footer>
  </div>
  {script_tag}
</body>
</html>"""


def _compact_page_head(*, title: str, subtitle: str, tag: str, snapshot: str = "") -> str:
    snap_html = f'<span class="updated-at">快照 {_escape(snapshot)}</span>' if snapshot else ""
    return (
        '<section class="page-head page-head--compact" aria-labelledby="page-title">'
        '<div class="page-title-group">'
        f'<h1 id="page-title">{_escape(title)}</h1>'
        f'<span class="updated-at">{_escape(subtitle)}</span>'
        "</div>"
        '<div class="page-meta">'
        f'<span class="demo-tag">{_escape(tag)}</span>'
        f"{snap_html}"
        "</div>"
        "</section>"
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
        "<tr>"
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
        f"<tbody>{body}</tbody></table></div>"
    )


def _overview_panels(metrics: dict[str, object]) -> str:
    """Legacy tabular summary panels retained for compatibility and diagnostics."""
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
        item_value = f"{account_id}\t{reader_id}"
        rows.append(
            "<tr>"
            '<td class="allowlist-cell-check"><input class="allowlist-check" type="checkbox" form="allowlist-batch-form" '
            f'name="item" value="{_escape(item_value)}" aria-label="选择 {_escape(reader_id)}"></td>'
            f'<td data-label="账户 ID"><code>{_escape(account_id)}</code></td>'
            f'<td class="allowlist-reader" data-label="读者 OpenID"><code>{_escape(reader_id)}</code></td>'
            f'<td data-label="来源">{_escape(source)}</td>'
            f'<td data-label="最近更新">{_escape(updated)}</td>'
            '<td class="allowlist-cell-action"><details class="trash-confirm">'
            f'<summary class="icon-btn icon-btn--danger" aria-label="移出 {_escape(reader_id)}" title="移出">'
            f"{_TRASH_ICON_SVG}</summary>"
            '<form class="trash-confirm-popover" method="post" action="/dashboard/admin/allowlist/remove">'
            f'<input type="hidden" name="account_id" value="{_escape(account_id)}">'
            f'<input type="hidden" name="reader_id" value="{_escape(reader_id)}">'
            '<input type="hidden" name="confirm" value="yes">'
            '<span class="trash-confirm-label">确认移出？</span>'
            '<button class="button button--danger button--xs" type="submit">确认</button>'
            "</form></details></td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="6">动态名单暂无条目。</td></tr>'


def _admin_allowlist_panel(store: SupportStore, pipeline: object, metrics: dict[str, object]) -> str:
    entries = store.list_allowlist(limit=200)
    dynamic_total = int(metrics.get("dynamic_allowlist_count", 0))
    static_total = len(getattr(pipeline, "auto_reply_allowlist", ()))
    batch_form = ""
    batch_controls = ""
    if entries:
        batch_form = (
            '<form id="allowlist-batch-form" method="post" action="/dashboard/admin/allowlist/remove-batch">'
            '<input type="hidden" name="confirm" value="yes">'
            "</form>"
        )
        batch_controls = (
            '<div class="allowlist-batch-actions">'
            '<details class="batch-confirm" id="batch-remove-details">'
            '<summary class="button button--danger-quiet">删除已选<span id="selected-count"></span></summary>'
            '<span class="trash-confirm-popover">'
            '<span class="trash-confirm-label">确认删除选中条目？</span>'
            '<button class="button button--danger button--xs" type="submit" form="allowlist-batch-form">确认删除</button>'
            "</span>"
            "</details>"
            "</div>"
        )
    table = (
        '<div class="table-wrap"><table class="data-table allowlist-table">'
        '<caption class="sr-only">动态自动回复允许名单</caption>'
        "<thead><tr>"
        '<th scope="col"><label class="allowlist-select-wrap">'
        '<input type="checkbox" id="allowlist-select-all" aria-label="全选动态条目">'
        '<span class="allowlist-select-label">全选</span></label></th>'
        '<th scope="col">账户 ID</th><th scope="col">读者 OpenID</th>'
        '<th scope="col">来源</th><th scope="col">最近更新</th><th scope="col">操作</th>'
        "</tr></thead>"
        f"<tbody>{_dynamic_allowlist_rows(entries)}</tbody></table></div>"
    )
    return (
        '<div class="dashboard-grid"><section class="panel panel--allowlist" aria-labelledby="allowlist-title">'
        '<div class="panel-head"><div><p class="eyebrow">管理员专属</p>'
        '<h2 id="allowlist-title">动态自动回复名单</h2>'
        f'<p class="panel-note">动态条目 {_count(dynamic_total)} 条，环境变量静态名单 {_count(static_total)} 条，只读。</p>'
        "</div></div>"
        '<p class="allowlist-explainer">添加或移除动态名单会影响后续回复。仅当自动回复与白名单门控开启时，名单成员才可能收到实发；关闭自动回复时仍为影子记录。静态环境变量名单不会在此页面修改。</p>'
        '<form class="allowlist-add-form" method="post" action="/dashboard/admin/allowlist/add">'
        '<label>账户 ID<input class="form-control" name="account_id" value="default" maxlength="255" required autocomplete="off"></label>'
        '<label>读者 OpenID<input class="form-control" name="reader_id" maxlength="255" required autocomplete="off"></label>'
        '<button class="button" type="submit">加入动态名单</button>'
        "</form>"
        f"{batch_form}"
        '<div class="allowlist-toolbar">'
        '<h3 class="subsection-title">最近更新的动态条目 <span>展示最多 200 条 · 勾选可批量删除</span></h3>'
        f"{batch_controls}"
        f"</div>{table}"
        '<details class="manual-remove-details">'
        "<summary>按账户 ID 与 OpenID 手动移除列表外条目</summary>"
        '<form class="allowlist-remove-known" method="post" action="/dashboard/admin/allowlist/remove">'
        '<label>账户 ID<input class="form-control" name="account_id" value="default" maxlength="255" required autocomplete="off"></label>'
        '<label>读者 OpenID<input class="form-control" name="reader_id" maxlength="255" required autocomplete="off"></label>'
        '<label class="confirm-label"><input type="checkbox" name="confirm" value="yes" required> 确认只移除该账户下该读者</label>'
        '<button class="button button--danger-quiet" type="submit">移除指定条目</button>'
        "</form>"
        "</details>"
        '<p class="panel-footnote">仅操作动态数据库名单，不修改静态环境变量配置。</p>'
        "</section></div>"
    )


_ADMIN_RESULT_NOTICES = {
    "added": "已加入动态名单。",
    "existing": "条目已存在，已更新来源。",
    "removed": "已移出动态名单。",
    "batch-removed": "已批量移出选中的动态名单条目。",
    "not-found": "没有找到该动态条目。",
    "invalid": "操作未执行，请先勾选条目或检查输入。",
}
_USER_RESULT_NOTICES: dict[str, tuple[str, str]] = {
    "created": ("馆员账号已创建。", "info"),
    "updated": ("账号权限与状态已保存。", "info"),
    "pw-updated": ("密码已重置，旧会话已全部失效。", "info"),
    "not-found": ("未找到该账号。", "error"),
    "invalid": ("操作未执行，请检查用户名（2–32位字母数字下划线）和密码（至少 6 位）。", "error"),
    "duplicate": ("该用户名已存在。", "error"),
}
_LIBRARIAN_RESULT_NOTICES: dict[str, tuple[str, str]] = {
    "closed": ("工单已标记结案。", "info"),
    "replied": ("微信回复已发送，工单已标记为「已处理」（读者致谢或超 24h 无追问自动结案）；后台正自动扫描可沉淀的 1. 标准模板。", "info"),
    "replied-closed": ("微信回复已发送，且工单已结案。", "info"),
    "replied-draft": ("微信回复已发送，并已生成 1. 标准模板草稿，请前往「标准模板」查看审核。", "info"),
    "replied-closed-draft": ("微信回复已发送、工单已结案，并已生成 1. 标准模板草稿。", "info"),
    "draft-created": ("已生成 1. 标准模板草稿，请前往「标准模板」查看审核。", "info"),
    "quota-blocked": ("已超 48 小时互动窗口或已达 5 条发送上限，无法通过接口发信，请前往微信公众号后台回复。", "error"),
    "send-failed": ("微信客服消息发送失败（工单状态未变更），请检查错误或前往微信公众号后台回复。", "error"),
    "not-found": ("未找到对应工单或私信，或状态已变更。", "error"),
    "invalid": ("操作未执行，请检查输入内容（单条回复不超过 1800 字节）或确认勾选。", "error"),
}
_PRESET_RESULT_NOTICES: dict[str, tuple[str, str]] = {
    "saved": ("模板草稿已保存（修改后需重新通过 AI 终审方可发布）。", "info"),
    "published": ("已通过确定性护栏与 AI 终审！1. 标准模板已写入动态目录并热重载生效。", "info"),
    "ai-rejected": ("发布被驳回：未通过隐私/个案/分类重合双审校验，请根据下方审核意见修改后重试。", "error"),
    "rejected": ("已将该模板草稿标记为驳回。", "info"),
    "not-found": ("未找到该模板草稿。", "error"),
    "invalid": ("草稿字段格式无效，请检查后重试。", "error"),
    "no-dir": ("运行时动态模板目录未配置，无法写入发布。", "error"),
}


def _admin_dashboard(
    pipeline: object,
    store: SupportStore,
    *,
    principal: DashboardPrincipal,
    result: str = "",
) -> str:
    metrics = store.overview_metrics()
    analytics = store.dashboard_admin_analytics()
    notice = _ADMIN_RESULT_NOTICES.get(result, "")
    date_range = str(analytics.get("date_range_label") or "近 7 日")
    snapshot = str(analytics.get("snapshot_at_utc8") or "")
    main = (
        _compact_page_head(
            title="服务总览",
            subtitle=f"近 7 日 {date_range}",
            tag="实时存储数据",
            snapshot=snapshot,
        )
        + (f'<p class="form-notice" role="status">{_escape(notice)}</p>' if notice else "")
        + render_admin_overview(analytics, reply_mode_text=_reply_mode(pipeline))
        + _admin_allowlist_panel(store, pipeline, metrics)
    )
    return _page_frame(
        principal=principal,
        active="admin",
        main=main,
        footer="管理员工作视图",
        script=_ADMIN_ALLOWLIST_SCRIPT,
    )


def _librarian_donut_adapter(
    segments: Sequence[tuple[str, int, str]],
    *,
    center_value: str,
    center_label: str,
    aria_label: str = "",
) -> str:
    chart_segments = [
        (name, color_cls, _COLOR_CLASS_TO_STROKE.get(color_cls, "var(--green)"), int(count))
        for name, count, color_cls in segments
    ]
    return render_donut(
        chart_segments,
        center_num=center_value,
        center_sub=center_label,
        aria_label=aria_label,
        small=True,
        stack=False,
    )


def _librarian_hbar_adapter(
    items: Sequence[tuple[str, int, str]],
    *,
    aria_label: str = "",
    show_stack_bar: bool = True,
) -> str:
    chart_items = [(label, color_cls, int(count)) for label, count, color_cls in items]
    return render_stack_hbar_list(
        chart_items,
        show_stack_bar=show_stack_bar,
        aria_label=aria_label,
    )


def _librarian_dashboard(
    pipeline: object,
    store: SupportStore,
    *,
    principal: DashboardPrincipal,
    before: int | None = None,
    active_view: str = "all",
    show_closed: bool = False,
    result: str = "",
) -> str:
    view = active_view if active_view in _VALID_LIBRARIAN_VIEWS else "all"
    can_reply = principal.has_permission(PERM_REPLY_READER)
    can_close = principal.has_permission(PERM_CLOSE_CASE)
    can_draft = principal.has_permission(PERM_DRAFT_PRESET)
    analytics = store.dashboard_librarian_analytics(include_closed=show_closed)
    today_stats = analytics.get("today_stats") if isinstance(analytics.get("today_stats"), Mapping) else {}
    total_created = int(today_stats.get("created", 0) or 0)
    total_closed = int(today_stats.get("closed", 0) or 0)
    inbox_html = (
        render_inbox(
            store,
            before=before,
            can_reply=can_reply,
            can_draft_preset=can_draft,
            active_view=view,
            show_closed=show_closed,
        )
        if view in ("all", "inbox")
        else ""
    )
    notice, notice_kind = _LIBRARIAN_RESULT_NOTICES.get(result, ("", "info"))
    snapshot = str(analytics.get("snapshot_at_utc8") or "")

    def _pace_adapter(hourly_pace: Sequence[Mapping[str, Any]]) -> str:
        return render_grouped_pace_chart(
            list(hourly_pace),
            total_created=total_created,
            total_closed=total_closed,
        )

    workbench = render_librarian_workbench(
        analytics,
        inbox_html=inbox_html,
        active_view=view,
        show_closed=show_closed,
        can_reply=can_reply,
        can_close=can_close,
        can_draft_preset=can_draft,
        notice=notice,
        notice_kind=notice_kind,
        render_sparkline_fn=lambda vals: render_sparkline(list(vals)),
        render_micro_bar_fn=lambda segs: render_micro_bar(list(segs)),
        render_donut_fn=_librarian_donut_adapter,
        render_pace_fn=_pace_adapter,
        render_hbar_fn=_librarian_hbar_adapter,
    )
    main = (
        _compact_page_head(
            title="待办咨询",
            subtitle="值班队列 · 支持直连回复与 1. 模板沉淀",
            tag="实时存储数据",
            snapshot=snapshot,
        )
        + _status_banner(_reply_mode(pipeline))
        + workbench
    )
    return _page_frame(principal=principal, active="librarian", main=main, footer="馆员工作视图")


def _valid_identifier(value: str) -> bool:
    return bool(value and len(value) <= _MAX_IDENTIFIER_LENGTH and all(character.isprintable() for character in value))


def _valid_resolution_note(value: str) -> bool:
    return len(value) <= _MAX_NOTE_LENGTH and all(
        character.isprintable() or character in "\t\n\r" for character in value
    )


def _valid_reply_text(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    return len(stripped.encode("utf-8")) <= _MAX_REPLY_UTF8_BYTES


async def _read_form(request: Request, *, max_bytes: int = _MAX_FORM_BYTES, max_fields: int = 12) -> dict[str, str] | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    try:
        raw = body.decode("utf-8")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=max_fields)
    except (UnicodeDecodeError, ValueError):
        return None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            return None
        values[key] = value
    return values


async def _read_form_multidict(
    request: Request,
    *,
    max_bytes: int = _MAX_RICH_FORM_BYTES,
    max_fields: int = 256,
) -> dict[str, list[str]] | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    try:
        raw = body.decode("utf-8")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=max_fields)
    except (UnicodeDecodeError, ValueError):
        return None
    values: dict[str, list[str]] = {}
    for key, value in pairs:
        values.setdefault(key, []).append(value)
    return values


def _csrf_valid(request: Request, settings: _DashboardSettings) -> bool:
    origin = _normalize_https_origin(request.headers.get("origin", ""))
    if origin != settings.public_origin:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site is None or fetch_site == "same-origin"


def _redirect_admin(result: str) -> RedirectResponse:
    return RedirectResponse(
        f"/dashboard/admin?result={result}",
        status_code=303,
        headers=_SECURITY_HEADERS,
    )


def _redirect_users(result: str) -> RedirectResponse:
    return RedirectResponse(
        f"/dashboard/admin/users?result={result}",
        status_code=303,
        headers=_SECURITY_HEADERS,
    )


def _redirect_librarian(result: str, *, view: str = "all", show_closed: bool = False) -> RedirectResponse:
    params: list[str] = []
    if view in ("cases", "inbox", "feedback"):
        params.append(f"view={view}")
    if show_closed:
        params.append("show=all")
    params.append(f"result={result}")
    return RedirectResponse(
        f"/dashboard/librarian?{'&'.join(params)}",
        status_code=303,
        headers=_SECURITY_HEADERS,
    )


def _redirect_presets(result: str, *, tab: str = "draft") -> RedirectResponse:
    safe_tab = tab if tab in _VALID_PRESET_TABS else "draft"
    return RedirectResponse(
        f"/dashboard/presets?tab={safe_tab}&result={result}",
        status_code=303,
        headers=_SECURITY_HEADERS,
    )


def _active_preset_catalog(request: Request) -> PresetCatalog:
    responder = _responder_from_request(request)
    catalog = getattr(responder, "preset_catalog", None)
    if isinstance(catalog, PresetCatalog):
        return catalog
    corpus_dir = Path(os.environ.get("SUPPORT_CORPUS_DIR", "rag-poc/corpus"))
    dyn_raw = os.environ.get("SUPPORT_DYNAMIC_PRESETS_DIR", "").strip()
    dynamic_dir = Path(dyn_raw) if dyn_raw else None
    try:
        return PresetCatalog.from_corpus_dirs(corpus_dir, dynamic_dir)
    except Exception:
        return PresetCatalog([])


def _dynamic_presets_dir(request: Request, store: SupportStore | None) -> Path | None:
    responder = _responder_from_request(request)
    dyn = getattr(responder, "dynamic_presets_dir", None)
    if isinstance(dyn, Path):
        return dyn
    dyn_raw = os.environ.get("SUPPORT_DYNAMIC_PRESETS_DIR", "").strip()
    if dyn_raw:
        return Path(dyn_raw)
    if store is not None:
        return store.db_path.parent / "presets"
    return None


def _auto_scan_preset_draft_sync(
    *,
    store: SupportStore,
    catalog: PresetCatalog,
    question: str,
    staff_reply: str,
    created_by: str,
    source_case_id: int | None = None,
    source_event_seq: int | None = None,
    llm_fn: PresetLlmFn | None = None,
) -> None:
    """Background scan evaluating whether a librarian reply should become a 1. preset draft."""
    try:
        existing_drafts = store.list_preset_drafts(status="draft", limit=100)
        if source_case_id is not None and any(d.get("source_case_id") == source_case_id for d in existing_drafts):
            return
        if source_event_seq is not None and any(d.get("source_event_seq") == source_event_seq for d in existing_drafts):
            return
        proposal = evaluate_and_generate_preset_proposal(
            question=question,
            staff_reply=staff_reply,
            catalog=catalog,
            llm_fn=llm_fn,
        )
        if proposal is None:
            return
        store.create_preset_draft(
            source_case_id=source_case_id,
            source_event_seq=source_event_seq,
            source_question=question,
            staff_reply=staff_reply,
            mode=proposal.mode,
            target_preset_id=proposal.target_preset_id,
            preset_id=proposal.preset_id,
            title=proposal.title,
            aliases=proposal.aliases,
            applicability=proposal.applicability,
            body=proposal.body,
            ai_generation_note=proposal.note,
            created_by=created_by,
        )
    except Exception:
        return


def create_dashboard_router(
    *,
    admin_password: str = "",
    librarian_password: str = "",
    public_origin: str = "",
    preset_generator_llm_fn: PresetLlmFn | None = None,
    preset_auditor_llm_fn: PresetLlmFn | None = None,
) -> APIRouter:
    """Create role- and permission-protected dashboard routes with Cookie + Basic auth."""
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
        stylesheet = _find_stylesheet_path()
        if stylesheet is None:
            return _plain_error(404, "not found")
        try:
            content = stylesheet.read_bytes()
        except OSError:
            return _plain_error(404, "not found")
        return Response(content=content, media_type="text/css; charset=utf-8", headers=_SECURITY_HEADERS)

    @router.get("/dashboard/login")
    async def login_page(request: Request) -> Response:
        if not _secure_public_request(request, settings):
            return _plain_error(404, "not found")
        store = _store_from_request(request)
        next_path = _safe_next_path(request.query_params.get("next", ""), default="/dashboard/librarian")
        principal = _principal_from_session(request, settings=settings, store=store)
        if principal is not None:
            dest = next_path if next_path != "/dashboard/librarian" else ("/dashboard/admin" if principal.is_admin else "/dashboard/librarian")
            return RedirectResponse(dest, status_code=303, headers=_SECURITY_HEADERS)
        err_code = request.query_params.get("err", "").strip()
        err_map = {
            "invalid": "账号或密码错误，或该账号已被停用。",
            "disabled": "该账号已被停用，请联系管理员。",
        }
        return _response(
            render_login_page(
                next_path=next_path,
                error=err_map.get(err_code, ""),
                style_tag=_inline_css_tag(),
                favicon_uri=_FAVICON_DATA_URI,
            )
        )

    @router.post("/dashboard/login")
    async def login_submit(request: Request) -> Response:
        if not _secure_public_request(request, settings):
            return _plain_error(404, "not found")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        username = form.get("username", "").strip()
        password = form.get("password", "")
        next_path = _safe_next_path(form.get("next", ""), default="")
        principal = _verify_credentials(username, password, settings=settings, store=store)
        if principal is None:
            return _response(
                render_login_page(
                    next_path=next_path or "/dashboard/librarian",
                    error="账号或密码错误，或该账号已被停用。",
                    style_tag=_inline_css_tag(),
                    favicon_uri=_FAVICON_DATA_URI,
                ),
                status_code=401,
            )
        token = store.create_dashboard_session(
            principal.username,
            principal.role,
            ttl_seconds=_SESSION_TTL_SECONDS,
        )
        default_dest = "/dashboard/admin" if principal.is_admin else "/dashboard/librarian"
        dest = next_path or default_dest
        if dest.startswith("/dashboard/admin") and not principal.is_admin:
            dest = "/dashboard/librarian"
        resp = RedirectResponse(dest, status_code=303, headers=_SECURITY_HEADERS)
        resp.set_cookie(
            key=_SESSION_COOKIE_NAME,
            value=token,
            max_age=_SESSION_TTL_SECONDS,
            path="/dashboard/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return resp

    @router.post("/dashboard/logout")
    async def logout_submit(request: Request) -> Response:
        if not _secure_public_request(request, settings):
            return _plain_error(404, "not found")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        store = _store_from_request(request)
        token = (request.cookies.get(_SESSION_COOKIE_NAME) or "").strip()
        if store is not None and token:
            store.delete_dashboard_session(token)
        resp = RedirectResponse("/dashboard/login", status_code=303, headers=_SECURITY_HEADERS)
        resp.delete_cookie(key=_SESSION_COOKIE_NAME, path="/dashboard/", secure=True, httponly=True, samesite="lax")
        return resp

    @router.get("/dashboard/librarian")
    async def librarian_dashboard(request: Request) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            required_permission=PERM_VIEW_WORKBENCH,
        )
        if error is not None or principal is None:
            return error or _plain_error(401, "authentication required")
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

        active_view = request.query_params.get("view", "all").strip().lower()
        if before is not None and active_view not in ("all", "inbox"):
            active_view = "inbox"
        show_closed = request.query_params.get("show", "").strip().lower() == "all"
        result = request.query_params.get("result", "").strip().lower()
        return _response(
            _librarian_dashboard(
                pipeline,
                store,
                principal=principal,
                before=before,
                active_view=active_view,
                show_closed=show_closed,
                result=result,
            )
        )

    @router.post("/dashboard/librarian/cases/{case_id}/reply")
    async def reply_case_from_dashboard(
        request: Request,
        case_id: int,
        background_tasks: BackgroundTasks,
    ) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            required_permission=PERM_REPLY_READER,
        )
        if error is not None or principal is None:
            return error or _plain_error(403, "permission denied")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request, max_bytes=16384, max_fields=12)
        if form is None:
            return _plain_error(400, "invalid form")
        pipeline = getattr(request.app.state, "support_pipeline", None)
        store = _store_from_request(request)
        if pipeline is None or store is None:
            return _plain_error(503, "dashboard data unavailable")

        view = form.get("view", "all").strip().lower()
        if view not in _VALID_LIBRARIAN_VIEWS:
            view = "all"
        show_closed = form.get("show", "").strip().lower() == "all"
        if case_id < 1 or case_id > _MAX_SQLITE_INTEGER:
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)

        reply_text = form.get("reply_text", "").strip()
        if not _valid_reply_text(reply_text):
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)

        target = store.get_staff_case_target(case_id)
        if target is None:
            return _redirect_librarian("not-found", view=view, show_closed=show_closed)

        quota = store.get_reader_reply_quota(target["account_id"], target["reader_id"])
        if not quota.get("can_reply"):
            return _redirect_librarian("quota-blocked", view=view, show_closed=show_closed)

        delivery = await pipeline.deliver_staff_reply(
            account_id=target["account_id"],
            reader_id=target["reader_id"],
            trigger_ref_id=case_id,
            content=reply_text,
            operator_username=principal.username,
        )
        if not delivery.get("ok"):
            return _redirect_librarian("send-failed", view=view, show_closed=show_closed)

        store.mark_staff_case_replied(case_id, reply_text, operator_username=principal.username)

        catalog = _active_preset_catalog(request)
        question = target["latest_message"] or target["summary"]
        background_tasks.add_task(
            _auto_scan_preset_draft_sync,
            store=store,
            catalog=catalog,
            question=question,
            staff_reply=reply_text,
            created_by=principal.username,
            source_case_id=case_id,
            llm_fn=preset_generator_llm_fn,
        )
        return _redirect_librarian("replied", view=view, show_closed=show_closed)

    @router.post("/dashboard/librarian/cases/{case_id}/draft-preset")
    async def draft_preset_from_case(request: Request, case_id: int) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            required_permission=PERM_DRAFT_PRESET,
        )
        if error is not None or principal is None:
            return error or _plain_error(403, "permission denied")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        view = form.get("view", "all").strip().lower()
        if view not in _VALID_LIBRARIAN_VIEWS:
            view = "all"
        show_closed = form.get("show", "").strip().lower() == "all"
        target = store.get_staff_case_target(case_id)
        if target is None:
            return _redirect_librarian("not-found", view=view, show_closed=show_closed)

        cases = store.list_staff_cases(limit=200)
        matched = next((c for c in cases if int(c["case_id"]) == case_id), None)
        staff_reply = ""
        if matched is not None:
            staff_msgs = [str(m.get("content") or "").strip() for m in matched.get("messages", []) if m.get("sender_role") == "staff" and str(m.get("content") or "").strip()]
            staff_reply = staff_msgs[-1] if staff_msgs else str(matched.get("resolution_note") or "").strip()
        if not staff_reply:
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)

        catalog = _active_preset_catalog(request)
        question = target["latest_message"] or target["summary"]
        proposal = await asyncio.to_thread(
            generate_preset_proposal,
            question=question,
            staff_reply=staff_reply,
            catalog=catalog,
            llm_fn=preset_generator_llm_fn,
        )
        store.create_preset_draft(
            source_case_id=case_id,
            source_question=question,
            staff_reply=staff_reply,
            mode=proposal.mode,
            target_preset_id=proposal.target_preset_id,
            preset_id=proposal.preset_id,
            title=proposal.title,
            aliases=proposal.aliases,
            applicability=proposal.applicability,
            body=proposal.body,
            ai_generation_note=proposal.note,
            created_by=principal.username,
        )
        return _redirect_librarian("draft-created", view=view, show_closed=show_closed)

    @router.post("/dashboard/librarian/inbox/{event_seq}/reply")
    async def reply_inbox_from_dashboard(
        request: Request,
        event_seq: int,
        background_tasks: BackgroundTasks,
    ) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            required_permission=PERM_REPLY_READER,
        )
        if error is not None or principal is None:
            return error or _plain_error(403, "permission denied")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request, max_bytes=16384, max_fields=12)
        if form is None:
            return _plain_error(400, "invalid form")
        pipeline = getattr(request.app.state, "support_pipeline", None)
        store = _store_from_request(request)
        if pipeline is None or store is None:
            return _plain_error(503, "dashboard data unavailable")

        view = form.get("view", "all").strip().lower()
        if view not in _VALID_LIBRARIAN_VIEWS:
            view = "all"
        show_closed = form.get("show", "").strip().lower() == "all"
        if event_seq < 1 or event_seq > _MAX_SQLITE_INTEGER:
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)

        reply_text = form.get("reply_text", "").strip()
        if not _valid_reply_text(reply_text):
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)

        target = store.get_inbox_event_target(event_seq)
        if target is None:
            return _redirect_librarian("not-found", view=view, show_closed=show_closed)

        quota = store.get_reader_reply_quota(target["account_id"], target["reader_id"])
        if not quota.get("can_reply"):
            return _redirect_librarian("quota-blocked", view=view, show_closed=show_closed)

        delivery = await pipeline.deliver_staff_reply(
            account_id=target["account_id"],
            reader_id=target["reader_id"],
            trigger_ref_id=event_seq,
            content=reply_text,
            operator_username=principal.username,
        )
        if not delivery.get("ok"):
            return _redirect_librarian("send-failed", view=view, show_closed=show_closed)

        linked_case_id: int | None = (
            int(target["staff_case_id"]) if target.get("staff_case_id") else None
        )
        if linked_case_id is None:
            latest_active = store.get_latest_active_staff_case(
                target["account_id"], target["reader_id"]
            )
            if latest_active is not None and latest_active.get("status") == "open":
                linked_case_id = int(latest_active["case_id"])
        if linked_case_id is not None:
            store.mark_staff_case_replied(
                linked_case_id,
                reply_text,
                operator_username=principal.username,
            )

        catalog = _active_preset_catalog(request)
        question = str(target.get("content") or "")
        background_tasks.add_task(
            _auto_scan_preset_draft_sync,
            store=store,
            catalog=catalog,
            question=question,
            staff_reply=reply_text,
            created_by=principal.username,
            source_case_id=linked_case_id,
            source_event_seq=event_seq,
            llm_fn=preset_generator_llm_fn,
        )
        return _redirect_librarian("replied", view=view, show_closed=show_closed)

    @router.post("/dashboard/librarian/cases/{case_id}/close")
    async def close_case_from_dashboard(request: Request, case_id: int) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            required_permission=PERM_CLOSE_CASE,
        )
        if error is not None or principal is None:
            return error or _plain_error(403, "permission denied")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        view = form.get("view", "all").strip().lower()
        if view not in _VALID_LIBRARIAN_VIEWS:
            view = "all"
        show_closed = form.get("show", "").strip().lower() == "all"
        if case_id < 1 or case_id > _MAX_SQLITE_INTEGER:
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)
        if form.get("confirm") != "yes":
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)
        resolution_note = form.get("resolution_note", "").strip()
        if not _valid_resolution_note(resolution_note):
            return _redirect_librarian("invalid", view=view, show_closed=show_closed)

        target = store.get_staff_case_target(case_id)
        if target is None or target.get("status") not in ("open", "replied"):
            return _redirect_librarian("not-found", view=view, show_closed=show_closed)
        closed = store.close_staff_case(
            case_id,
            resolution_note=resolution_note,
            closed_by=principal.username,
        )
        if closed is None or closed.status != "closed":
            return _redirect_librarian("not-found", view=view, show_closed=show_closed)
        return _redirect_librarian("closed", view=view, show_closed=show_closed)

    @router.get("/dashboard/admin")
    async def admin_dashboard(request: Request) -> Response:
        principal, error = _authenticate_principal(request, settings, require_admin=True)
        if error is not None or principal is None:
            return error or _plain_error(403, "administrator access required")
        pipeline = getattr(request.app.state, "support_pipeline", None)
        store = _store_from_request(request)
        if pipeline is None or store is None:
            return _plain_error(503, "dashboard data unavailable")
        return _response(
            _admin_dashboard(
                pipeline,
                store,
                principal=principal,
                result=request.query_params.get("result", ""),
            ),
            headers=_ADMIN_SECURITY_HEADERS,
        )

    @router.get("/dashboard/admin/users")
    async def admin_users_page(request: Request) -> Response:
        principal, error = _authenticate_principal(request, settings, require_admin=True)
        if error is not None or principal is None:
            return error or _plain_error(403, "administrator access required")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")
        users = store.list_dashboard_users()
        notice, notice_kind = _USER_RESULT_NOTICES.get(
            request.query_params.get("result", "").strip().lower(),
            ("", "info"),
        )
        main = (
            _compact_page_head(
                title="馆员账号与权限管理",
                subtitle="独立账号登录 · 细粒度权限即时生效",
                tag="管理员专属",
            )
            + render_admin_users_page(
                users,
                current_username=principal.username,
                notice=notice,
                notice_kind=notice_kind,
            )
        )
        return _response(_page_frame(principal=principal, active="users", main=main, footer="账号权限管理"))

    @router.post("/dashboard/admin/users/create")
    async def admin_create_user(request: Request) -> Response:
        _, error = _authenticate_principal(request, settings, require_admin=True)
        if error is not None:
            return error
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form_multidict(request, max_bytes=8192, max_fields=32)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        username = (form.get("username") or [""])[0].strip()
        display_name = (form.get("display_name") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        role = (form.get("role") or ["librarian"])[0].strip().lower()
        perms = normalize_permissions(form.get("perm") or ())
        if not valid_username(username) or len(password) < 6 or role not in ("admin", "librarian"):
            return _redirect_users("invalid")
        if store.get_dashboard_user(username) is not None:
            return _redirect_users("duplicate")
        try:
            store.create_dashboard_user(
                username=username,
                password=password,
                display_name=display_name,
                role=role,
                permissions=ALL_LIBRARIAN_PERMISSIONS if role == "admin" else perms,
                is_active=True,
            )
        except ValueError:
            return _redirect_users("invalid")
        return _redirect_users("created")

    @router.post("/dashboard/admin/users/{username}/update")
    async def admin_update_user(request: Request, username: str) -> Response:
        _, error = _authenticate_principal(request, settings, require_admin=True)
        if error is not None:
            return error
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form_multidict(request, max_bytes=8192, max_fields=32)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")
        if not valid_username(username):
            return _redirect_users("invalid")

        display_name = (form.get("display_name") or [""])[0].strip()
        role = (form.get("role") or ["librarian"])[0].strip().lower()
        is_active = (form.get("is_active") or ["1"])[0].strip() == "1"
        perms = normalize_permissions(form.get("perm") or ())
        if role not in ("admin", "librarian"):
            return _redirect_users("invalid")
        updated = store.update_dashboard_user(
            username,
            display_name=display_name,
            role=role,
            permissions=ALL_LIBRARIAN_PERMISSIONS if role == "admin" else perms,
            is_active=is_active,
        )
        if updated is None:
            return _redirect_users("not-found")
        return _redirect_users("updated")

    @router.post("/dashboard/admin/users/{username}/password")
    async def admin_reset_user_password(request: Request, username: str) -> Response:
        _, error = _authenticate_principal(request, settings, require_admin=True)
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
        new_password = form.get("new_password", "")
        if not valid_username(username) or len(new_password) < 6:
            return _redirect_users("invalid")
        ok = store.set_dashboard_user_password(username, new_password)
        return _redirect_users("pw-updated" if ok else "not-found")

    @router.get("/dashboard/presets")
    async def presets_review_page(request: Request) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            any_permissions=(PERM_REVIEW_PRESET, PERM_DRAFT_PRESET),
        )
        if error is not None or principal is None:
            return error or _plain_error(403, "permission denied")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        tab = request.query_params.get("tab", "draft").strip().lower()
        if tab not in _VALID_PRESET_TABS:
            tab = "draft"
        drafts = store.list_preset_drafts(limit=100)
        catalog = _active_preset_catalog(request)
        notice, notice_kind = _PRESET_RESULT_NOTICES.get(
            request.query_params.get("result", "").strip().lower(),
            ("", "info"),
        )
        main = (
            _compact_page_head(
                title="1. 标准模板审核与管理",
                subtitle="AI 转换归类 · 确定性护栏 + AI 终审双保险 · 方案 A 热重载",
                tag="热重载就绪",
            )
            + render_presets_review_page(
                drafts,
                catalog.presets,
                active_tab=tab,
                can_review=principal.has_permission(PERM_REVIEW_PRESET),
                notice=notice,
                notice_kind=notice_kind,
            )
        )
        return _response(_page_frame(principal=principal, active="presets", main=main, footer="标准模板审核"))

    @router.post("/dashboard/presets/drafts/{draft_id}/save")
    async def mutate_preset_draft(request: Request, draft_id: int) -> Response:
        principal, error = _authenticate_principal(
            request,
            settings,
            any_permissions=(PERM_REVIEW_PRESET, PERM_DRAFT_PRESET),
        )
        if error is not None or principal is None:
            return error or _plain_error(403, "permission denied")
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form(request, max_bytes=_MAX_RICH_FORM_BYTES, max_fields=24)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        existing = store.get_preset_draft(draft_id)
        if existing is None or existing.get("status") != "draft":
            return _redirect_presets("not-found", tab="draft")

        intent = form.get("intent", "save").strip().lower()
        if intent in ("publish", "reject") and not principal.has_permission(PERM_REVIEW_PRESET):
            return _plain_error(403, "preset review permission required")

        mode = form.get("mode", "create").strip().lower()
        if mode not in ("create", "merge"):
            mode = "create"
        target_preset_id = form.get("target_preset_id", "").strip()
        preset_id = form.get("preset_id", "").strip()
        if mode == "merge" and target_preset_id:
            preset_id = target_preset_id
        title = form.get("title", "").strip()
        raw_aliases = form.get("aliases", "")
        aliases = tuple(line.strip() for line in raw_aliases.splitlines() if line.strip())
        applicability = form.get("applicability", "").strip()
        body = form.get("body", "").strip()

        store.update_preset_draft(
            draft_id,
            mode=mode,
            target_preset_id=target_preset_id if mode == "merge" else "",
            preset_id=preset_id,
            title=title,
            aliases=aliases,
            applicability=applicability,
            body=body,
        )

        if intent == "reject":
            store.reject_preset_draft(draft_id, reviewed_by=principal.username)
            return _redirect_presets("rejected", tab="rejected")

        if intent == "save":
            return _redirect_presets("saved", tab="draft")

        if intent != "publish":
            return _redirect_presets("invalid", tab="draft")

        catalog = _active_preset_catalog(request)
        audit = await asyncio.to_thread(
            audit_preset_for_publish,
            mode=mode,
            target_preset_id=target_preset_id if mode == "merge" else "",
            preset_id=preset_id,
            title=title,
            aliases=aliases,
            applicability=applicability,
            body=body,
            catalog=catalog,
            llm_fn=preset_auditor_llm_fn,
        )
        store.set_preset_draft_review(
            draft_id,
            ai_review_status="passed" if audit.passed else "rejected",
            reasons=audit.reasons,
            suggestion=audit.suggestion,
            reviewed_by=principal.username,
        )
        if not audit.passed:
            return _redirect_presets("ai-rejected", tab="draft")

        dynamic_dir = _dynamic_presets_dir(request, store)
        if dynamic_dir is None:
            return _redirect_presets("no-dir", tab="draft")

        await asyncio.to_thread(
            write_dynamic_preset,
            dynamic_dir,
            preset_id=preset_id,
            title=title,
            aliases=aliases,
            applicability=applicability,
            body=body,
        )
        responder = _responder_from_request(request)
        if responder is not None and hasattr(responder, "reload_presets"):
            await asyncio.to_thread(responder.reload_presets)
        store.mark_preset_draft_published(draft_id, reviewed_by=principal.username)
        return _redirect_presets("published", tab="published")

    async def mutate_allowlist(request: Request, *, operation: str) -> Response:
        _, error = _authenticate_principal(request, settings, require_admin=True)
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
            return _redirect_admin("invalid")
        if operation == "remove":
            if form.get("confirm") != "yes":
                return _redirect_admin("invalid")
            removed = store.remove_allowlist(reader_id, account_id=account_id)
            return _redirect_admin("removed" if removed else "not-found")
        added = store.enroll_allowlist(
            account_id=account_id,
            reader_id=reader_id,
            source="admin",
        )
        return _redirect_admin("added" if added else "existing")

    @router.post("/dashboard/admin/allowlist/add")
    async def add_allowlist(request: Request) -> Response:
        return await mutate_allowlist(request, operation="add")

    @router.post("/dashboard/admin/allowlist/remove")
    async def remove_allowlist(request: Request) -> Response:
        return await mutate_allowlist(request, operation="remove")

    @router.post("/dashboard/admin/allowlist/remove-batch")
    async def remove_allowlist_batch(request: Request) -> Response:
        _, error = _authenticate_principal(request, settings, require_admin=True)
        if error is not None:
            return error
        if not _csrf_valid(request, settings):
            return _plain_error(403, "same-origin request required")
        form = await _read_form_multidict(request)
        if form is None:
            return _plain_error(400, "invalid form")
        store = _store_from_request(request)
        if store is None:
            return _plain_error(503, "dashboard data unavailable")

        confirms = form.get("confirm", [])
        if len(confirms) != 1 or confirms[0] != "yes":
            return _redirect_admin("invalid")
        raw_items = form.get("item", [])
        if not raw_items or len(raw_items) > 200:
            return _redirect_admin("invalid")

        targets: list[tuple[str, str]] = []
        for raw_item in raw_items:
            account_id, sep, reader_id = raw_item.partition("\t")
            account_id = account_id.strip()
            reader_id = reader_id.strip()
            if not sep or not _valid_identifier(account_id) or not _valid_identifier(reader_id):
                return _redirect_admin("invalid")
            targets.append((account_id, reader_id))

        removed_total = 0
        for account_id, reader_id in targets:
            removed_total += store.remove_allowlist(reader_id, account_id=account_id)
        return _redirect_admin("batch-removed" if removed_total > 0 else "not-found")

    return router


router = create_dashboard_router(
    admin_password=os.environ.get("SUPPORT_DASHBOARD_ADMIN_PASSWORD", ""),
    librarian_password=os.environ.get("SUPPORT_DASHBOARD_LIBRARIAN_PASSWORD", ""),
    public_origin=os.environ.get("SUPPORT_DASHBOARD_PUBLIC_ORIGIN", ""),
)
