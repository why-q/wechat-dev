"""Server-rendered librarian and administrator dashboards for the support store."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hmac
import html
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

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
from .db import SupportStore


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
    "Vary": "Authorization",
}
_FAVICON_DATA_URI = (
    "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E"
    "%3Crect width=%2732%27 height=%2732%27 rx=%275%27 fill=%27%231e4334%27/%3E"
    "%3Ctext x=%2716%27 y=%2722%27 text-anchor=%27middle%27 font-size=%2718%27 "
    "font-family=%27sans-serif%27 fill=%27%23fcfbf7%27%3E%E9%A6%86%3C/text%3E%3C/svg%3E"
)
_MAX_FORM_BYTES = 4096
_MAX_IDENTIFIER_LENGTH = 255
_MAX_NOTE_LENGTH = 500
_MAX_SQLITE_INTEGER = 2**63 - 1
_VALID_LIBRARIAN_VIEWS = frozenset({"all", "cases", "inbox", "feedback"})
_COLOR_CLASS_TO_STROKE = {
    "c-green": "var(--green)",
    "c-teal": "var(--teal)",
    "c-ochre": "var(--ochre)",
    "c-rust": "var(--rust)",
    "c-ink": "var(--ink)",
    "c-slate": "var(--slate)",
    "c-muted": "oklch(0.76 0.015 110)",
}


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
        password
        for role, password in (
            ("admin", settings.admin_password),
            ("librarian", settings.librarian_password),
        )
        if role in allowed_roles
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
    home_href = "/dashboard/admin" if role == "admin" and active == "admin" else "/dashboard/librarian"
    style_tag = _inline_css_tag()
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
      <nav class="role-switch" aria-label="工作视图">{navigation}</nav>
    </header>
    <main id="main-content">{main}</main>
    <footer class="footer"><span>{_escape(footer)}</span><span>仅授权馆员访问 · 页面不缓存</span></footer>
  </div>
</body>
</html>"""


def _compact_page_head(*, title: str, subtitle: str, tag: str, snapshot: str) -> str:
    return (
        '<section class="page-head page-head--compact" aria-labelledby="page-title">'
        '<div class="page-title-group">'
        f'<h1 id="page-title">{_escape(title)}</h1>'
        f'<span class="updated-at">{_escape(subtitle)}</span>'
        "</div>"
        '<div class="page-meta">'
        f'<span class="demo-tag">{_escape(tag)}</span>'
        f'<span class="updated-at">快照 {_escape(snapshot)}</span>'
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
        rows.append(
            "<tr>"
            f'<td><code>{_escape(account_id)}</code></td>'
            f'<td class="allowlist-reader"><code>{_escape(reader_id)}</code></td>'
            f"<td>{_escape(source)}</td>"
            f"<td>{_escape(updated)}</td>"
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
        f'<h3 class="subsection-title">最近更新的动态条目 <span>展示最多 200 条，旧条目可按账户 ID 与 OpenID 指定移除</span></h3>{table}'
        '<form class="allowlist-remove-known" method="post" action="/dashboard/admin/allowlist/remove">'
        '<h3 class="subsection-title">移除列表外条目</h3>'
        '<label>账户 ID<input class="form-control" name="account_id" value="default" maxlength="255" required autocomplete="off"></label>'
        '<label>读者 OpenID<input class="form-control" name="reader_id" maxlength="255" required autocomplete="off"></label>'
        '<label class="confirm-label"><input type="checkbox" name="confirm" value="yes" required> 确认只移除该账户下该读者</label>'
        '<button class="button button--quiet" type="submit">移除指定条目</button>'
        "</form>"
        '<p class="panel-footnote">不会清空名单，也不会修改静态环境变量配置。</p>'
        "</section></div>"
    )


_ADMIN_RESULT_NOTICES = {
    "added": "已加入动态名单。",
    "existing": "条目已存在，已更新来源。",
    "removed": "已移出动态名单。",
    "not-found": "没有找到该动态条目。",
    "invalid": "操作未执行，请检查输入。",
}
_LIBRARIAN_RESULT_NOTICES = {
    "closed": "工单已标记结案。",
    "not-found": "未找到该未结工单，或工单已被结案。",
    "invalid": "结案操作未执行，请确认勾选并检查备注内容。",
}


def _admin_dashboard(pipeline: object, store: SupportStore, *, result: str = "") -> str:
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
    return _page_frame(role="admin", active="admin", main=main, footer="管理员工作视图")


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
    role: str,
    before: int | None = None,
    active_view: str = "all",
    result: str = "",
) -> str:
    view = active_view if active_view in _VALID_LIBRARIAN_VIEWS else "all"
    analytics = store.dashboard_librarian_analytics()
    today_stats = analytics.get("today_stats") if isinstance(analytics.get("today_stats"), Mapping) else {}
    total_created = int(today_stats.get("created", 0) or 0)
    total_closed = int(today_stats.get("closed", 0) or 0)
    inbox_html = render_inbox(store, before=before) if view in ("all", "inbox") else ""
    notice = _LIBRARIAN_RESULT_NOTICES.get(result, "")
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
        notice=notice,
        render_sparkline_fn=lambda vals: render_sparkline(list(vals)),
        render_micro_bar_fn=lambda segs: render_micro_bar(list(segs)),
        render_donut_fn=_librarian_donut_adapter,
        render_pace_fn=_pace_adapter,
        render_hbar_fn=_librarian_hbar_adapter,
    )
    main = (
        _compact_page_head(
            title="待办咨询",
            subtitle="值班队列 · 公众号后台回复后在此结案",
            tag="实时存储数据",
            snapshot=snapshot,
        )
        + _status_banner(_reply_mode(pipeline))
        + workbench
    )
    return _page_frame(role=role, active="librarian", main=main, footer="馆员工作视图")


def _valid_identifier(value: str) -> bool:
    return bool(value and len(value) <= _MAX_IDENTIFIER_LENGTH and all(character.isprintable() for character in value))


def _valid_resolution_note(value: str) -> bool:
    return len(value) <= _MAX_NOTE_LENGTH and all(
        character.isprintable() or character in "\t\n\r" for character in value
    )


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


def _redirect_admin(result: str) -> RedirectResponse:
    return RedirectResponse(
        f"/dashboard/admin?result={result}",
        status_code=303,
        headers=_SECURITY_HEADERS,
    )


def _redirect_librarian(result: str, *, view: str = "all") -> RedirectResponse:
    query = f"result={result}"
    if view in ("cases", "inbox", "feedback"):
        query = f"view={view}&{query}"
    return RedirectResponse(
        f"/dashboard/librarian?{query}",
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
        stylesheet = _find_stylesheet_path()
        if stylesheet is None:
            return _plain_error(404, "not found")
        try:
            content = stylesheet.read_bytes()
        except OSError:
            return _plain_error(404, "not found")
        return Response(content=content, media_type="text/css; charset=utf-8", headers=_SECURITY_HEADERS)

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

        active_view = request.query_params.get("view", "all").strip().lower()
        if before is not None and active_view not in ("all", "inbox"):
            active_view = "inbox"
        result = request.query_params.get("result", "").strip().lower()
        return _response(
            _librarian_dashboard(
                pipeline,
                store,
                role=role or "librarian",
                before=before,
                active_view=active_view,
                result=result,
            )
        )

    @router.post("/dashboard/librarian/cases/{case_id}/close")
    async def close_case_from_dashboard(request: Request, case_id: int) -> Response:
        _, error = _authenticate(
            request,
            settings,
            allowed_roles=frozenset({"admin", "librarian"}),
        )
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

        view = form.get("view", "all").strip().lower()
        if view not in _VALID_LIBRARIAN_VIEWS:
            view = "all"
        if case_id < 1 or case_id > _MAX_SQLITE_INTEGER:
            return _redirect_librarian("invalid", view=view)
        if form.get("confirm") != "yes":
            return _redirect_librarian("invalid", view=view)
        resolution_note = form.get("resolution_note", "").strip()
        if not _valid_resolution_note(resolution_note):
            return _redirect_librarian("invalid", view=view)

        open_ids = {int(item["case_id"]) for item in store.list_staff_cases(limit=200, status="open")}
        if case_id not in open_ids:
            return _redirect_librarian("not-found", view=view)
        closed = store.close_staff_case(case_id, resolution_note=resolution_note)
        if closed is None or closed.status != "closed":
            return _redirect_librarian("not-found", view=view)
        return _redirect_librarian("closed", view=view)

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

    return router


router = create_dashboard_router(
    admin_password=os.environ.get("SUPPORT_DASHBOARD_ADMIN_PASSWORD", ""),
    librarian_password=os.environ.get("SUPPORT_DASHBOARD_LIBRARIAN_PASSWORD", ""),
    public_origin=os.environ.get("SUPPORT_DASHBOARD_PUBLIC_ORIGIN", ""),
)
