"""Display-only librarian inbox rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from .db import SupportStore

PAGE_SIZE = 50
_MESSAGE_TYPES = {
    "text": "文本消息",
    "image": "图片消息",
    "voice": "语音消息",
    "video": "视频消息",
    "shortvideo": "短视频消息",
    "location": "位置消息",
    "link": "链接消息",
    "file": "文件消息",
}


def render_inbox(store: SupportStore, *, before: int | None = None) -> str:
    """Render retained reader DMs as unclosed, without changing support state."""
    rows = store.list_librarian_inbox(before=before, limit=PAGE_SIZE + 1)
    has_older = len(rows) > PAGE_SIZE
    page = rows[:PAGE_SIZE]
    if not page:
        return '<section class="panel queue-panel"><h1>私信按未结案展示</h1><p>当前没有保留中的读者私信。</p></section>'

    items: list[str] = []
    for row in page:
        content = str(row["content"] or "").strip()
        msg_type = str(row["msg_type"] or "").strip().lower()
        label = _MESSAGE_TYPES.get(msg_type, "其他消息")
        category = str(row["filter_category"] or "")
        if category == "gray_enroll":
            label = "灰度入组消息"
            content = "灰度入组消息，内容不展示"
        elif not content:
            content = f"[{label}]"
        stamp = datetime.fromisoformat(str(row["received_at_utc"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        timestamp = stamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        items.append(
            '<li class="case-row"><div class="case-main">'
            f'<h2>{escape(str(row["reader_id"]))}</h2>'
            f'<p class="case-meta"><span>{escape(timestamp)}</span><span>{escape(label)}</span></p>'
            f'<p class="thread">{escape(content)}</p></div>'
            '<span class="case-state">未结案</span></li>'
        )

    paging = ""
    if has_older:
        paging = f'<nav aria-label="私信分页"><a href="?before={int(page[-1]["seq"])}">更早私信</a></nav>'
    return (
        '<section class="panel queue-panel"><h1>私信按未结案展示</h1>'
        '<p>所有保留中的读者私信均按未结案显示，仅供查看。</p>'
        f'<ul class="case-list">{"".join(items)}</ul>{paging}</section>'
    )
