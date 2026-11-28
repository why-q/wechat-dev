"""Pure server-rendered HTML pages for dashboard login, RBAC user admin, and `1.` preset dual-review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import html
from typing import Any
from urllib.parse import quote

from .auth import (
    ALL_LIBRARIAN_PERMISSIONS,
    DEFAULT_LIBRARIAN_PERMISSIONS,
    PERMISSION_LABELS,
)
from .preset_workflow import compute_preset_diff

_VALID_PRESET_TABS = frozenset({"draft", "published", "rejected", "active"})

_NOTICE_CLASS_BY_KIND = {
    "error": ("form-notice form-notice--error", "alert"),
    "warn": ("form-notice form-notice--warn", "status"),
    "warning": ("form-notice form-notice--warn", "status"),
    "info": ("form-notice", "status"),
    "success": ("form-notice", "status"),
}

_PRESET_EMPTY_NOTES = {
    "draft": "当前没有待审核的模板草稿。",
    "published": "暂无已发布的模板草稿记录。",
    "rejected": "暂无已驳回的模板草稿记录。",
}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_path_segment(value: object) -> str:
    return quote(str(value or "").strip(), safe="._-")


def _safe_draft_id(raw: object) -> str:
    try:
        return str(int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return _safe_path_segment(raw)


def _normalize_str_list(raw: Any) -> list[str]:
    if raw is None or isinstance(raw, (str, bytes)):
        return []
    if not isinstance(raw, Sequence) and not isinstance(raw, (set, frozenset)):
        return []
    items: list[str] = []
    for entry in raw:
        text = str(entry).strip()
        if text:
            items.append(text)
    return items


def _render_notice(notice: str, notice_kind: str = "info") -> str:
    clean = str(notice or "").strip()
    if not clean:
        return ""
    kind = str(notice_kind or "info").strip().lower()
    css_class, role = _NOTICE_CLASS_BY_KIND.get(kind, ("form-notice", "status"))
    return f'<p class="{css_class}" role="{role}">{_esc(clean)}</p>'


def _render_permission_checkboxes(selected: set[str] | frozenset[str]) -> str:
    chips: list[str] = []
    for perm in ALL_LIBRARIAN_PERMISSIONS:
        label = PERMISSION_LABELS.get(perm, perm)
        checked = " checked" if perm in selected else ""
        chips.append(
            '<label class="perm-chip">'
            f'<input type="checkbox" name="perm" value="{_esc(perm)}"{checked}>'
            f"<span>{_esc(label)}</span>"
            "</label>"
        )
    return f'<div class="perm-grid">{"".join(chips)}</div>'


def render_login_page(
    *,
    next_path: str = "/dashboard/librarian",
    error: str = "",
    style_tag: str = "",
    favicon_uri: str = "",
) -> str:
    """Render a standalone, zero-JS responsive HTML login page for dashboard operators."""
    safe_next = str(next_path or "/dashboard/librarian")
    clean_error = str(error or "").strip()
    error_html = (
        f'<p class="form-notice form-notice--error" role="alert">{_esc(clean_error)}</p>'
        if clean_error
        else ""
    )
    favicon_html = f'<link rel="icon" href="{_esc(favicon_uri)}">\n  ' if favicon_uri else ""
    style_html = f"{style_tag}\n" if style_tag else ""
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="color-scheme" content="light">\n'
        '  <meta name="theme-color" content="#f3f1e9">\n'
        '  <meta name="referrer" content="strict-origin">\n'
        "  <title>图书馆智能客服 · 登录工作台</title>\n"
        f"  {favicon_html}{style_html}"
        "</head>\n"
        "<body>\n"
        '  <div class="login-shell">\n'
        '    <main class="login-card" aria-labelledby="login-title">\n'
        '      <div class="login-brand">\n'
        '        <span class="brand">\n'
        '          <span class="brand-mark" aria-hidden="true">馆</span>\n'
        '          <span class="brand-name">图书馆智能客服</span>\n'
        "        </span>\n"
        '        <h1 id="login-title">登录工作台</h1>\n'
        "      </div>\n"
        f"      {error_html}\n"
        '      <form class="login-form" method="post" action="/dashboard/login">\n'
        f'        <input type="hidden" name="next" value="{_esc(safe_next)}">\n'
        "        <label>账号"
        '<input class="form-control" name="username" required autocomplete="username">'
        "</label>\n"
        "        <label>密码"
        '<input class="form-control" type="password" name="password" required autocomplete="current-password">'
        "</label>\n"
        '        <button class="button login-submit" type="submit">登录工作台</button>\n'
        "      </form>\n"
        '      <p class="login-footer">使用馆员独立账号或管理员账号登录 · 会话仅限 HTTPS 传输</p>\n'
        "    </main>\n"
        "  </div>\n"
        "</body>\n"
        "</html>"
    )


def render_admin_users_page(
    users: Sequence[Mapping[str, Any]],
    *,
    current_username: str,
    notice: str = "",
    notice_kind: str = "info",
) -> str:
    """Render pure HTML fragment for administrator user & RBAC permission management."""
    notice_html = _render_notice(notice, notice_kind)
    default_perms = frozenset(DEFAULT_LIBRARIAN_PERMISSIONS)
    clean_current = str(current_username or "").strip()

    create_panel = (
        '<section class="panel panel--span-12" aria-labelledby="user-create-title">'
        '<div class="panel-head">'
        "<div>"
        '<p class="eyebrow">管理员专属 · 账号管理</p>'
        '<h2 id="user-create-title">新建馆员/管理员账号</h2>'
        f'<p class="panel-note">当前操作账号：<code>{_esc(clean_current)}</code> · 管理员角色默认拥有全部权限</p>'
        "</div>"
        "</div>"
        '<form class="user-create-form" method="post" action="/dashboard/admin/users/create">'
        '<div class="user-form-fields">'
        "<label>账号（2-32位字母/数字/下划线）"
        '<input class="form-control" name="username" minlength="2" maxlength="32" '
        'pattern="[a-zA-Z0-9_.-]{2,32}" required autocomplete="off" placeholder="如 lib_zhang">'
        "</label>"
        "<label>显示姓名"
        '<input class="form-control" name="display_name" maxlength="64" autocomplete="off" placeholder="如 张馆员">'
        "</label>"
        "<label>初始密码（至少 6 位）"
        '<input class="form-control" type="password" name="password" minlength="6" required '
        'autocomplete="new-password" placeholder="至少 6 位字符">'
        "</label>"
        "<label>角色"
        '<select class="form-control" name="role">'
        '<option value="librarian" selected>普通馆员</option>'
        '<option value="admin">管理员</option>'
        "</select>"
        "</label>"
        "</div>"
        '<div class="user-perms-block">'
        '<span class="perm-label">权限配置</span>'
        f"{_render_permission_checkboxes(default_perms)}"
        '<p class="panel-note">管理员角色默认拥有全部权限</p>'
        "</div>"
        '<div class="user-form-actions">'
        '<button class="button" type="submit">创建账号</button>'
        "</div>"
        "</form>"
        "</section>"
    )

    user_list = [u for u in users if isinstance(u, Mapping)]
    if not user_list:
        list_body = '<p class="panel-note">当前暂无数据库馆员账号（环境变量超级管理员始终可用）。</p>'
    else:
        cards: list[str] = []
        for user in user_list:
            username = str(user.get("username") or "").strip()
            display_name = str(user.get("display_name") or "").strip()
            role = str(user.get("role") or "librarian").strip()
            is_active = bool(user.get("is_active", True))
            last_login_raw = user.get("last_login_utc")
            last_login = str(last_login_raw).strip() if last_login_raw else "从未登录"
            user_perms = set(_normalize_str_list(user.get("permissions")))

            role_badge = (
                '<span class="badge ochre">管理员</span>'
                if role == "admin"
                else '<span class="badge muted">普通馆员</span>'
            )
            status_badge = (
                '<span class="badge good">启用</span>'
                if is_active
                else '<span class="badge muted">已停用</span>'
            )
            self_badge = (
                '<span class="badge muted">当前登录</span>'
                if clean_current and username == clean_current
                else ""
            )
            display_html = f'<span class="user-display-name">{_esc(display_name)}</span>' if display_name else ""

            action_user = _esc(_safe_path_segment(username))
            sel_lib = " selected" if role != "admin" else ""
            sel_admin = " selected" if role == "admin" else ""
            sel_active = " selected" if is_active else ""
            sel_inactive = " selected" if not is_active else ""

            cards.append(
                '<article class="user-card">'
                '<div class="user-card-head">'
                '<div class="user-card-identity">'
                f"<code>{_esc(username)}</code>"
                f"{display_html}"
                f"{role_badge}"
                f"{status_badge}"
                f"{self_badge}"
                "</div>"
                f'<span class="queue-time">最近登录：{_esc(last_login)}</span>'
                "</div>"
                f'<form class="user-edit-form" method="post" action="/dashboard/admin/users/{action_user}/update">'
                '<div class="user-edit-fields">'
                "<label>显示姓名"
                f'<input class="form-control" name="display_name" value="{_esc(display_name)}" maxlength="64" autocomplete="off">'
                "</label>"
                "<label>角色"
                '<select class="form-control" name="role">'
                f'<option value="librarian"{sel_lib}>普通馆员</option>'
                f'<option value="admin"{sel_admin}>管理员</option>'
                "</select>"
                "</label>"
                "<label>账号状态"
                '<select class="form-control" name="is_active">'
                f'<option value="1"{sel_active}>启用</option>'
                f'<option value="0"{sel_inactive}>已停用</option>'
                "</select>"
                "</label>"
                "</div>"
                '<div class="user-perms-block">'
                f"{_render_permission_checkboxes(user_perms)}"
                "</div>"
                '<div class="user-form-actions">'
                '<button class="button button--quiet" type="submit">保存权限与状态</button>'
                "</div>"
                "</form>"
                '<details class="queue-details">'
                "<summary>重置密码</summary>"
                f'<form class="user-pw-form" method="post" action="/dashboard/admin/users/{action_user}/password">'
                '<input class="form-control" type="password" name="new_password" minlength="6" required '
                'autocomplete="new-password" placeholder="输入新密码（至少 6 位）">'
                '<button class="button button--quiet button--xs" type="submit">更新密码并强制重新登录</button>'
                "</form>"
                "</details>"
                "</article>"
            )
        list_body = f'<div class="user-list">{"".join(cards)}</div>'

    list_panel = (
        '<section class="panel panel--span-12" aria-labelledby="user-list-title">'
        '<div class="panel-head">'
        "<div>"
        '<p class="eyebrow">数据库账号</p>'
        '<h2 id="user-list-title">账号与权限列表</h2>'
        "</div>"
        f'<span class="period-chip">共 {len(user_list)} 个账号</span>'
        "</div>"
        f"{list_body}"
        "</section>"
    )

    return f'{notice_html}<div class="dashboard-grid">{create_panel}{list_panel}</div>'


def _normalize_active_preset(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        preset_id = str(item.get("preset_id") or item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        aliases = _normalize_str_list(item.get("aliases"))
        applicability = str(item.get("applicability") or "").strip()
        body = str(item.get("body") or "").strip()
        is_dynamic = bool(item.get("is_dynamic", False))
    else:
        preset_id = str(getattr(item, "preset_id", "") or "").strip()
        title = str(getattr(item, "title", "") or "").strip()
        aliases = _normalize_str_list(getattr(item, "aliases", ()))
        applicability = str(getattr(item, "applicability", "") or "").strip()
        body = str(getattr(item, "body", "") or "").strip()
        is_dynamic = bool(getattr(item, "is_dynamic", False))
    return {
        "preset_id": preset_id,
        "title": title,
        "aliases": aliases,
        "applicability": applicability,
        "body": body,
        "is_dynamic": is_dynamic,
    }


def _render_alias_tags(aliases: Sequence[str]) -> str:
    if not aliases:
        return '<span class="panel-note">无触发别名</span>'
    tags = "".join(f'<span class="badge muted">{_esc(alias)}</span>' for alias in aliases)
    return f'<div class="preset-aliases">{tags}</div>'


def _render_ai_review_badge(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean == "passed":
        return '<span class="badge good">AI 通过</span>'
    if clean in ("rejected", "failed"):
        return '<span class="badge urgent">AI 驳回</span>'
    return '<span class="badge muted">待 AI 终审</span>'


def _render_audit_box(ai_status: str, reasons: Sequence[str], suggestion: str) -> str:
    clean_reasons = [str(r).strip() for r in reasons if str(r).strip()]
    clean_suggestion = str(suggestion or "").strip()
    if not clean_reasons and not clean_suggestion:
        return ""
    is_passed = str(ai_status or "").strip().lower() == "passed"
    box_class = "audit-box audit-box--passed" if is_passed else "audit-box audit-box--rejected"
    heading = "AI 终审结果：通过" if is_passed else "AI 终审驳回详情"
    reasons_html = ""
    if clean_reasons:
        items = "".join(f"<li>{_esc(reason)}</li>" for reason in clean_reasons)
        reasons_html = f'<ul class="audit-reasons">{items}</ul>'
    suggestion_html = (
        f'<p class="audit-suggestion"><strong>修改建议：</strong>{_esc(clean_suggestion)}</p>'
        if clean_suggestion
        else ""
    )
    return (
        f'<div class="{box_class}" role="status">'
        f'<p class="audit-box-title">{_esc(heading)}</p>'
        f"{reasons_html}"
        f"{suggestion_html}"
        "</div>"
    )


def _render_merge_diff_box(old_preset: Mapping[str, Any], new_body: str) -> str:
    old_body = str(old_preset.get("body") or "")
    entries = compute_preset_diff(old_body, new_body)
    lines: list[str] = []
    for entry in entries:
        kind = entry.get("kind", "same")
        text = entry.get("text", "")
        if kind == "add":
            lines.append(f'<div class="diff-line diff-line--add">+ {_esc(text)}</div>')
        elif kind == "del":
            lines.append(f'<div class="diff-line diff-line--del">- {_esc(text)}</div>')
        else:
            lines.append(f'<div class="diff-line diff-line--same">  {_esc(text)}</div>')
    target_title = str(old_preset.get("title") or "")
    target_id = str(old_preset.get("preset_id") or "")
    header = (
        f'<div class="diff-box-head">合并对比（基准：{_esc(target_title)} '
        f"<code>{_esc(target_id)}</code>）</div>"
    )
    return (
        '<div class="diff-box" aria-label="合并前后正文对比">'
        f"{header}"
        f'<div class="diff-lines">{"".join(lines)}</div>'
        "</div>"
    )


def _render_active_presets_panel(active_presets: Sequence[Mapping[str, Any]]) -> str:
    header = (
        '<div class="panel-head">'
        "<div>"
        '<p class="eyebrow">热重载生效目录</p>'
        '<h2 id="active-presets-title">当前生效 1. 模板</h2>'
        "</div>"
        f'<span class="period-chip">{len(active_presets)} 条</span>'
        "</div>"
    )
    if not active_presets:
        return (
            '<section class="panel panel--span-12" aria-labelledby="active-presets-title">'
            f"{header}"
            '<p class="panel-note">当前暂无生效的 1. 模板。</p>'
            "</section>"
        )

    cards: list[str] = []
    for preset in active_presets:
        preset_id = str(preset["preset_id"])
        title = str(preset["title"] or preset_id or "未命名模板")
        source_badge = (
            '<span class="badge good">动态发布</span>'
            if preset["is_dynamic"]
            else '<span class="badge muted">仓库基线</span>'
        )
        aliases_html = _render_alias_tags(preset["aliases"])
        applicability = str(preset["applicability"] or "")
        app_html = (
            f'<p class="preset-applicability"><strong>适用条件：</strong>{_esc(applicability)}</p>'
            if applicability
            else ""
        )
        body = str(preset["body"] or "")
        cards.append(
            '<article class="draft-card preset-live-card">'
            '<div class="draft-card-head">'
            '<div class="draft-card-title-group">'
            f"<h3>{_esc(title)}</h3>"
            f"<code>{_esc(preset_id)}</code>"
            f"{source_badge}"
            "</div>"
            "</div>"
            f"{aliases_html}"
            f"{app_html}"
            '<details class="queue-details">'
            "<summary>查看正文</summary>"
            f'<pre class="preset-body-pre">{_esc(body)}</pre>'
            "</details>"
            "</article>"
        )
    return (
        '<section class="panel panel--span-12" aria-labelledby="active-presets-title">'
        f'{header}<div class="preset-list">{"".join(cards)}</div>'
        "</section>"
    )


def render_presets_review_page(
    drafts: Sequence[Mapping[str, Any]],
    active_presets: Sequence[Any],
    *,
    active_tab: str = "draft",
    can_review: bool = False,
    notice: str = "",
    notice_kind: str = "info",
) -> str:
    """Render pure HTML fragment for `1.` preset drafts, AI dual-review, merge diffs, and live presets."""
    tab = active_tab if active_tab in _VALID_PRESET_TABS else "draft"
    normalized_active = [_normalize_active_preset(item) for item in active_presets]
    active_by_id = {p["preset_id"]: p for p in normalized_active if p["preset_id"]}

    draft_items = [d for d in drafts if isinstance(d, Mapping)]
    counts = {
        "draft": sum(1 for d in draft_items if str(d.get("status") or "").strip() == "draft"),
        "published": sum(1 for d in draft_items if str(d.get("status") or "").strip() == "published"),
        "rejected": sum(1 for d in draft_items if str(d.get("status") or "").strip() == "rejected"),
        "active": len(normalized_active),
    }

    tabs = (
        ("draft", "/dashboard/presets?tab=draft", "待审核草稿", counts["draft"]),
        ("published", "/dashboard/presets?tab=published", "已发布", counts["published"]),
        ("rejected", "/dashboard/presets?tab=rejected", "已驳回", counts["rejected"]),
        ("active", "/dashboard/presets?tab=active", "当前生效 1. 模板", counts["active"]),
    )
    tab_links: list[str] = []
    for key, href, label, count in tabs:
        current = ' aria-current="page"' if tab == key else ""
        tab_links.append(
            f'<a href="{href}"{current}>{_esc(label)} <span class="view-tab-count">{count}</span></a>'
        )
    tabs_html = f'<nav class="view-tabs" aria-label="模板审核视图">{"".join(tab_links)}</nav>'
    notice_html = _render_notice(notice, notice_kind)

    if tab == "active":
        content_html = _render_active_presets_panel(normalized_active)
        return f'{tabs_html}{notice_html}<div class="dashboard-grid">{content_html}</div>'

    filtered = [d for d in draft_items if str(d.get("status") or "").strip() == tab]
    if not filtered:
        empty_note = _PRESET_EMPTY_NOTES.get(tab, "暂无记录。")
        empty_panel = (
            '<section class="panel panel--span-12">'
            f'<p class="panel-note">{_esc(empty_note)}</p>'
            "</section>"
        )
        return f'{tabs_html}{notice_html}<div class="dashboard-grid">{empty_panel}</div>'

    cards: list[str] = []
    for draft in filtered:
        draft_id_raw = draft.get("draft_id", "")
        safe_did = _esc(_safe_draft_id(draft_id_raw))
        title = str(draft.get("title") or "").strip()
        mode = str(draft.get("mode") or "create").strip().lower()
        target_preset_id = str(draft.get("target_preset_id") or "").strip()
        preset_id = str(draft.get("preset_id") or "").strip()
        aliases = _normalize_str_list(draft.get("aliases"))
        applicability = str(draft.get("applicability") or "").strip()
        body = str(draft.get("body") or "")
        created_by = str(draft.get("created_by") or "").strip() or "未知"
        created_at = str(draft.get("created_at_utc") or "").strip()
        reviewed_by = str(draft.get("reviewed_by") or "").strip()
        published_at = str(draft.get("published_at_utc") or "").strip()
        source_question = str(draft.get("source_question") or "").strip()
        staff_reply = str(draft.get("staff_reply") or "").strip()
        ai_note = str(draft.get("ai_generation_note") or "").strip()
        ai_status = str(draft.get("ai_review_status") or "pending").strip().lower()
        ai_reasons = _normalize_str_list(draft.get("ai_review_reasons"))
        ai_suggestion = str(draft.get("ai_review_suggestion") or "").strip()

        mode_badge = (
            '<span class="badge ochre">并入已有模板</span>'
            if mode == "merge"
            else '<span class="badge good">新建模板</span>'
        )
        ai_badge = _render_ai_review_badge(ai_status)

        source_parts: list[str] = []
        if source_question:
            source_parts.append(
                f'<div class="queue-thread"><strong>原读者提问：</strong>{_esc(source_question)}</div>'
            )
        if staff_reply:
            source_parts.append(
                f'<div class="queue-thread"><strong>馆员原答复：</strong>{_esc(staff_reply)}</div>'
            )
        if ai_note:
            source_parts.append(
                f'<div class="queue-thread"><strong>AI 生成说明：</strong>{_esc(ai_note)}</div>'
            )
        source_body = "".join(source_parts) or '<p class="panel-note">无来源上下文记录。</p>'
        source_details = (
            '<details class="queue-details">'
            "<summary>查看来源问答与 AI 生成说明</summary>"
            f"{source_body}"
            "</details>"
        )

        audit_html = _render_audit_box(ai_status, ai_reasons, ai_suggestion)

        diff_html = ""
        if mode == "merge" and target_preset_id and target_preset_id in active_by_id:
            diff_html = _render_merge_diff_box(active_by_id[target_preset_id], body)

        if tab == "draft":
            sel_create = " selected" if mode != "merge" else ""
            sel_merge = " selected" if mode == "merge" else ""
            target_options = ['<option value="">（新建模式无需选择）</option>']
            seen_targets: set[str] = set()
            for ap in normalized_active:
                ap_id = str(ap["preset_id"])
                ap_title = str(ap["title"] or ap_id)
                if not ap_id:
                    continue
                seen_targets.add(ap_id)
                selected_attr = " selected" if ap_id == target_preset_id else ""
                target_options.append(
                    f'<option value="{_esc(ap_id)}"{selected_attr}>{_esc(ap_title)} ({_esc(ap_id)})</option>'
                )
            if target_preset_id and target_preset_id not in seen_targets:
                target_options.append(
                    f'<option value="{_esc(target_preset_id)}" selected>{_esc(target_preset_id)}（未在当前生效列表）</option>'
                )

            review_buttons = ""
            if can_review:
                review_buttons = (
                    '<button class="button" type="submit" name="intent" value="publish">AI 终审并发布生效</button>'
                    '<button class="button button--danger-quiet" type="submit" name="intent" value="reject">驳回草稿</button>'
                )

            aliases_text = "\n".join(aliases)
            body_section = (
                f'<form class="draft-edit-form" method="post" action="/dashboard/presets/drafts/{safe_did}/save">'
                '<div class="draft-form-grid">'
                "<label>发布模式"
                '<select class="form-control" name="mode">'
                f'<option value="create"{sel_create}>新建模板 (create)</option>'
                f'<option value="merge"{sel_merge}>并入已有模板 (merge)</option>'
                "</select>"
                "</label>"
                "<label>并入目标模板"
                '<select class="form-control" name="target_preset_id">'
                f'{"".join(target_options)}'
                "</select>"
                "</label>"
                "<label>模板 ID (preset_id)"
                f'<input class="form-control" name="preset_id" value="{_esc(preset_id)}" maxlength="64" required>'
                "</label>"
                "<label>模板标题"
                f'<input class="form-control" name="title" value="{_esc(title)}" maxlength="120" required>'
                "</label>"
                "</div>"
                "<label>触发别名（每行一条）"
                f'<textarea class="form-control" name="aliases" rows="3">{_esc(aliases_text)}</textarea>'
                "</label>"
                "<label>适用条件"
                f'<textarea class="form-control" name="applicability" rows="2">{_esc(applicability)}</textarea>'
                "</label>"
                "<label>回复正文"
                f'<textarea class="form-control" name="body" rows="6">{_esc(body)}</textarea>'
                "</label>"
                '<div class="draft-form-actions">'
                '<button class="button button--quiet" type="submit" name="intent" value="save">保存修改</button>'
                f"{review_buttons}"
                "</div>"
                "</form>"
            )
        else:
            meta_bits: list[str] = [f"模板 ID：<code>{_esc(preset_id)}</code>"]
            if mode == "merge" and target_preset_id:
                meta_bits.append(f"并入目标：<code>{_esc(target_preset_id)}</code>")
            if reviewed_by:
                meta_bits.append(f"审核人：{_esc(reviewed_by)}")
            if published_at:
                meta_bits.append(f"发布时间：{_esc(published_at)}")
            body_section = (
                '<div class="draft-readonly">'
                f'<p class="panel-note">{" · ".join(meta_bits)}</p>'
                f"{_render_alias_tags(aliases)}"
                f'<p class="preset-applicability"><strong>适用条件：</strong>{_esc(applicability)}</p>'
                f'<pre class="preset-body-pre">{_esc(body)}</pre>'
                "</div>"
            )

        cards.append(
            '<article class="panel panel--span-12 draft-card">'
            '<div class="draft-card-head">'
            '<div class="draft-card-title-group">'
            f'<span class="draft-id">#{_esc(draft_id_raw)}</span>'
            f'<h3>{_esc(title or "未命名草稿")}</h3>'
            f"{mode_badge}"
            f"{ai_badge}"
            "</div>"
            f'<span class="queue-time">提交人：{_esc(created_by)}'
            f'{f" · {_esc(created_at)}" if created_at else ""}</span>'
            "</div>"
            f"{source_details}"
            f"{audit_html}"
            f"{diff_html}"
            f"{body_section}"
            "</article>"
        )

    return f'{tabs_html}{notice_html}<div class="dashboard-grid">{"".join(cards)}</div>'
