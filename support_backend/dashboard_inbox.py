"""Librarian inbox rendering (compact list with optional 48h direct reply)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from html import escape

from .db import SupportStore

PAGE_SIZE = 50
_SHANGHAI = timezone(timedelta(hours=8))
_VALID_VIEWS = frozenset({"all", "cases", "inbox", "feedback"})
_NON_TEXT_TYPES = {
    "image": "图片消息",
    "voice": "语音消息",
    "video": "视频消息",
    "shortvideo": "短视频消息",
    "location": "位置消息",
    "link": "链接消息",
    "file": "文件消息",
}


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _format_remaining_short(seconds: int) -> str:
    secs = max(0, int(seconds))
    if secs >= 3600:
        return f"{secs // 3600}h"
    if secs > 0:
        return f"{max(1, (secs + 59) // 60)}m"
    return "0m"


def _format_reply_hm(raw_stamp: object) -> str:
    text = str(raw_stamp or "").strip()
    if not text:
        return ""
    if len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit():
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_SHANGHAI).strftime("%H:%M")


def _inbox_paging_href(*, before: int | None, active_view: str, show_closed: bool) -> str:
    params: list[str] = []
    if before is not None:
        params.append(f"before={int(before)}")
    if active_view != "all":
        params.append(f"view={active_view}")
    if show_closed:
        params.append("show=all")
    if before is not None:
        return f"?{'&'.join(params)}"
    if params:
        return f"/dashboard/librarian?{'&'.join(params)}"
    return "/dashboard/librarian"


def _render_staff_replies(row: Mapping[str, object]) -> str:
    raw_replies = row.get("staff_replies")
    if not isinstance(raw_replies, Sequence) or isinstance(raw_replies, (str, bytes)):
        return ""
    parts: list[str] = []
    for rep in raw_replies:
        if not isinstance(rep, Mapping):
            continue
        content = str(rep.get("content") or "").strip()
        if not content:
            continue
        operator = str(rep.get("operator_username") or "").strip() or "馆员"
        hm = _format_reply_hm(rep.get("created_hm") or rep.get("created_at_utc") or "")
        who = f"{operator} {hm}" if hm else operator
        status = str(rep.get("status") or "").strip().lower()
        prefix = "发送失败" if status == "failed" else "已回复"
        parts.append(f'<p class="inbox-reply-log">{prefix}（{_esc(who)}）：{_esc(content)}</p>')
    return "".join(parts)


def _render_inbox_reply_control(
    row: Mapping[str, object],
    *,
    can_draft_preset: bool,
    active_view: str,
    show_closed: bool,
) -> str:
    seq = _safe_int(row.get("seq"), 0)
    raw_quota = row.get("reply_quota")
    quota = raw_quota if isinstance(raw_quota, Mapping) else {}
    quota_can_reply = bool(quota.get("can_reply", False))
    sent = max(0, _safe_int(quota.get("sent_in_window"), 0))
    limit = max(1, _safe_int(quota.get("quota_limit"), 5))
    rem_sec = max(0, _safe_int(quota.get("window_remaining_seconds"), 0))
    blocked_reason = str(quota.get("blocked_reason") or "").strip().lower()
    window_open = bool(quota.get("window_open", False))

    if quota_can_reply:
        if rem_sec > 0:
            summary_text = f"回复（剩{_format_remaining_short(rem_sec)}·{sent}/{limit}）"
        else:
            summary_text = "回复"
        show_val = "all" if show_closed else "open"
        draft_html = (
            '<label class="confirm-label"><input type="checkbox" name="save_preset_draft" value="yes"> '
            "同时生成 1. 标准模板草稿</label>"
            if can_draft_preset
            else ""
        )
        return (
            '<details class="inbox-reply-details">'
            f"<summary>{_esc(summary_text)}</summary>"
            f'<form class="reply-form" method="post" action="/dashboard/librarian/inbox/{seq}/reply">'
            f'<input type="hidden" name="view" value="{_esc(active_view)}">'
            f'<input type="hidden" name="show" value="{show_val}">'
            '<textarea class="form-control reply-textarea" name="reply_text" rows="2" maxlength="900" '
            'required placeholder="输入要直接发送给读者的微信回复（≤1800字节）"></textarea>'
            '<div class="reply-form-actions">'
            f"{draft_html}"
            '<button class="button button--xs" type="submit">发送微信回复</button>'
            "</div>"
            "</form>"
            "</details>"
        )

    blocked_tag = "额度满" if (blocked_reason == "quota_exhausted" or (window_open and sent >= limit)) else "超48h"
    return (
        '<details class="inbox-reply-details">'
        f'<summary><span class="inbox-quota-blocked">{blocked_tag}</span></summary>'
        '<p class="panel-note">已超 48 小时回复窗口或已达 5 条上限，请前往微信公众号后台回复。</p>'
        "</details>"
    )


def render_inbox(
    store: SupportStore,
    *,
    before: int | None = None,
    can_reply: bool = False,
    can_draft_preset: bool = False,
    active_view: str = "all",
    show_closed: bool = False,
) -> str:
    """Render retained reader DMs compactly on one line, with optional 48h direct reply controls."""
    view = active_view if active_view in _VALID_VIEWS else "all"
    rows = store.list_librarian_inbox(before=before, limit=PAGE_SIZE + 1)
    has_older = len(rows) > PAGE_SIZE
    page = rows[:PAGE_SIZE]
    latest_href = _inbox_paging_href(before=None, active_view=view, show_closed=show_closed)
    if not page:
        latest = (
            f'<nav class="inbox-paging" aria-label="私信分页"><a href="{latest_href}">返回最新</a></nav>'
            if before is not None
            else ""
        )
        chip = "支持 48h 内直复" if can_reply else "仅查看 · 未结案"
        return (
            '<section class="panel queue-panel">'
            f'<div class="panel-head"><h2>读者私信</h2><span class="period-chip">{chip}</span></div>'
            f'<p class="panel-note">当前没有保留中的读者私信。</p>{latest}</section>'
        )

    items: list[str] = []
    for row in page:
        content = str(row["content"] or "").strip()
        msg_type = str(row["msg_type"] or "").strip().lower()
        category = str(row["filter_category"] or "")
        badge_html = ""
        if category == "gray_enroll":
            badge_html = '<span class="badge muted">灰度入组消息</span>'
            content = "灰度入组消息，内容不展示"
        elif category == "emergency":
            badge_html = '<span class="badge urgent">紧急消息</span>'
            if not content:
                content = "[紧急消息]"
        elif msg_type and msg_type != "text":
            type_label = _NON_TEXT_TYPES.get(msg_type, "其他消息")
            badge_html = f'<span class="badge muted">{_esc(type_label)}</span>'
            if not content:
                content = f"[{type_label}]"
        elif not content:
            content = "[空消息]"

        stamp = datetime.fromisoformat(str(row["received_at_utc"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        local = stamp.astimezone(_SHANGHAI)
        short_time = local.strftime("%m-%d %H:%M")
        full_time = local.strftime("%Y-%m-%d %H:%M UTC+8")
        iso_time = local.isoformat(timespec="minutes")

        reply_logs_html = _render_staff_replies(row) if can_reply else ""
        reply_control_html = (
            _render_inbox_reply_control(
                row,
                can_draft_preset=can_draft_preset,
                active_view=view,
                show_closed=show_closed,
            )
            if can_reply
            else ""
        )

        items.append(
            '<li class="case-row case-row--compact">'
            f'<time class="inbox-time" datetime="{_esc(iso_time)}" title="{_esc(full_time)}">{_esc(short_time)}</time>'
            f'<p class="inbox-text">{badge_html}{_esc(content)}</p>'
            '<span class="case-state">未结案</span>'
            f"{reply_logs_html}"
            f"{reply_control_html}"
            "</li>"
        )

    paging = (
        f'<nav class="inbox-paging" aria-label="私信分页"><a href="{latest_href}">返回最新</a></nav>'
        if before is not None
        else ""
    )
    if has_older:
        older_href = _inbox_paging_href(before=int(page[-1]["seq"]), active_view=view, show_closed=show_closed)
        paging = (
            '<nav class="inbox-paging" aria-label="私信分页">'
            f'<a href="{older_href}">更早私信</a>'
            f'<a href="{latest_href}">返回最新</a>'
            "</nav>"
        )
    chip = f"本页 {len(page)} 条 · 支持 48h 内直复" if can_reply else f"本页 {len(page)} 条 · 按未结案展示"
    return (
        '<section class="panel queue-panel">'
        '<div class="panel-head">'
        "<h2>读者私信</h2>"
        f'<span class="period-chip">{chip}</span>'
        "</div>"
        f'<ul class="case-list">{"".join(items)}</ul>{paging}</section>'
    )
