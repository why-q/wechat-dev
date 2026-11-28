"""Display-only librarian inbox rendering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from .db import SupportStore

PAGE_SIZE = 50
_SHANGHAI = timezone(timedelta(hours=8))
_NON_TEXT_TYPES = {
    "image": "图片消息",
    "voice": "语音消息",
    "video": "视频消息",
    "shortvideo": "短视频消息",
    "location": "位置消息",
    "link": "链接消息",
    "file": "文件消息",
}


def render_inbox(store: SupportStore, *, before: int | None = None) -> str:
    """Render retained reader DMs compactly on one line, without changing support state."""
    rows = store.list_librarian_inbox(before=before, limit=PAGE_SIZE + 1)
    has_older = len(rows) > PAGE_SIZE
    page = rows[:PAGE_SIZE]
    if not page:
        latest = (
            '<nav class="inbox-paging" aria-label="私信分页"><a href="/dashboard/librarian">返回最新</a></nav>'
            if before is not None
            else ""
        )
        return (
            '<section class="panel queue-panel">'
            '<div class="panel-head"><h2>读者私信</h2><span class="period-chip">仅查看 · 未结案</span></div>'
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
            badge_html = f'<span class="badge muted">{escape(type_label)}</span>'
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
        items.append(
            '<li class="case-row case-row--compact">'
            f'<time class="inbox-time" datetime="{escape(iso_time)}" title="{escape(full_time)}">{escape(short_time)}</time>'
            f'<p class="inbox-text">{badge_html}{escape(content)}</p>'
            '<span class="case-state">未结案</span>'
            "</li>"
        )

    paging = (
        '<nav class="inbox-paging" aria-label="私信分页"><a href="/dashboard/librarian">返回最新</a></nav>'
        if before is not None
        else ""
    )
    if has_older:
        paging = (
            '<nav class="inbox-paging" aria-label="私信分页">'
            f'<a href="?before={int(page[-1]["seq"])}">更早私信</a>'
            '<a href="/dashboard/librarian">返回最新</a>'
            "</nav>"
        )
    return (
        '<section class="panel queue-panel">'
        '<div class="panel-head">'
        "<h2>读者私信</h2>"
        f'<span class="period-chip">本页 {len(page)} 条 · 按未结案展示</span>'
        "</div>"
        f'<ul class="case-list">{"".join(items)}</ul>{paging}</section>'
    )
