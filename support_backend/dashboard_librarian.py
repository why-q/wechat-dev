"""Server-rendered Librarian Workbench components (cases queue, close forms, feedback, charts)."""

from __future__ import annotations

from html import escape
import math
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote


_VALID_VIEWS = frozenset({"all", "cases", "inbox", "feedback"})

_CASE_KIND_LABELS = {
    "staff_todo": "待办",
    "emergency": "紧急",
    "feedback": "反馈",
    "staff_action": "人工跟进",
}

_SENDER_ROLE_LABELS = {
    "reader": "读者",
    "user": "读者",
    "staff": "馆员",
    "librarian": "馆员",
    "admin": "馆员",
    "system": "系统",
    "assistant": "系统",
}

_FEEDBACK_STATUS_LABELS = {
    "open": ("待阅", "ochre"),
    "new": ("待阅", "ochre"),
    "pending": ("待阅", "ochre"),
    "reviewed": ("已阅", "good"),
    "closed": ("已处理", "good"),
    "resolved": ("已处理", "good"),
}

_STROKE_BY_CLASS = {
    "c-rust": "var(--rust)",
    "c-ochre": "var(--ochre)",
    "c-teal": "var(--teal)",
    "c-green": "var(--green)",
    "c-ink": "var(--ink)",
    "c-slate": "var(--slate)",
    "c-muted": "oklch(0.76 0.015 110)",
}

_HM_PATTERN = re.compile(r"\b(\d{2}:\d{2})\b")


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return num if math.isfinite(num) else None


def _truncate(text: str, limit: int = 48) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(1, limit - 1)]}…"


def _extract_hm(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    match = _HM_PATTERN.search(text)
    return match.group(1) if match else text


def _format_avg_minutes(value: float) -> str:
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


def _safe_case_id(raw: object) -> str:
    try:
        return str(int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return quote(str(raw or "").strip(), safe="")


def _default_micro_bar(segments: Sequence[tuple[str, float]]) -> str:
    spans: list[str] = []
    for color_class, pct in segments:
        clamped = max(0.0, min(100.0, float(pct)))
        if clamped <= 0.0:
            continue
        spans.append(f'<span class="{_esc(color_class)}" style="--w: {clamped:.1f}%"></span>')
    return f'<div class="micro-bar" aria-hidden="true">{"".join(spans)}</div>'


def _default_sparkline(values: Sequence[int | float]) -> str:
    nums: list[float] = []
    for item in values:
        parsed = _float_or_none(item)
        if parsed is not None:
            nums.append(parsed)
    if len(nums) < 2:
        return ""
    lo = min(nums)
    hi = max(nums)
    span = hi - lo
    n = len(nums)
    points: list[tuple[float, float]] = []
    for idx, val in enumerate(nums):
        x = 2.0 + (60.0 * idx) / (n - 1)
        if span <= 0.0:
            y = 12.0
        else:
            y = 20.0 - ((val - lo) / span) * 16.0
        points.append((round(x, 1), round(y, 1)))
    line_pts = " ".join(f"{x},{y}" for x, y in points)
    area_pts = f"{line_pts} {points[-1][0]},22 {points[0][0]},22"
    last_x, last_y = points[-1]
    return (
        '<svg class="metric-spark" viewBox="0 0 64 24" aria-hidden="true">'
        f'<polygon class="svg-area" points="{area_pts}" />'
        f'<polyline class="svg-line" points="{line_pts}" />'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.2" fill="var(--ink)" />'
        "</svg>"
    )


def _default_donut(
    segments: Sequence[tuple[str, int, str]],
    *,
    center_value: str,
    center_label: str,
    aria_label: str = "",
) -> str:
    cleaned = [(str(label), max(0, _int(count)), str(color)) for label, count, color in segments]
    total = sum(count for _, count, _ in cleaned)
    circumference = 2.0 * math.pi * 36.0
    nonzero_count = sum(1 for _, count, _ in cleaned if count > 0)
    gap = 2.0 if nonzero_count > 1 else 0.0

    arcs: list[str] = []
    offset = 0.0
    if total > 0:
        for _, count, color_class in cleaned:
            if count <= 0:
                continue
            seg_len = circumference * (count / total)
            draw_len = max(1.0, seg_len - gap)
            stroke = _STROKE_BY_CLASS.get(color_class, "var(--green)")
            arcs.append(
                f'<circle cx="50" cy="50" r="36" fill="none" stroke="{stroke}" '
                f'stroke-width="11" stroke-dasharray="{draw_len:.2f} {circumference:.2f}" '
                f'stroke-dashoffset="{-offset:.2f}" />'
            )
            offset += seg_len

    legend_items: list[str] = []
    for label, count, color_class in cleaned:
        pct = round((count / total) * 100) if total > 0 else 0
        legend_items.append(
            "<li>"
            f'<span class="swatch {_esc(color_class)}"></span>'
            f'<span class="donut-name">{_esc(label)}</span>'
            f'<span class="donut-val">{count}</span>'
            f'<span class="donut-pct">{pct}%</span>'
            "</li>"
        )

    label_attr = f' aria-label="{_esc(aria_label)}"' if aria_label else ""
    arcs_group = f'<g transform="rotate(-90 50 50)">{"".join(arcs)}</g>' if arcs else ""
    return (
        '<div class="donut-layout">'
        f'<svg class="donut-svg donut-svg--sm" viewBox="0 0 100 100" role="img"{label_attr}>'
        '<circle class="donut-track" cx="50" cy="50" r="36" stroke-width="11" />'
        f"{arcs_group}"
        f'<text class="donut-center-num" x="50" y="49" font-size="18">{_esc(center_value)}</text>'
        f'<text class="donut-center-sub" x="50" y="62">{_esc(center_label)}</text>'
        "</svg>"
        f'<ul class="donut-legend">{"".join(legend_items)}</ul>'
        "</div>"
    )


def _default_pace_chart(hourly_pace: Sequence[Mapping[str, Any]]) -> str:
    rows = [
        (
            str(item.get("label") or f"{_int(item.get('hour')):02d}:00"),
            max(0, _int(item.get("created"))),
            max(0, _int(item.get("closed"))),
        )
        for item in hourly_pace
    ]
    total_created = sum(created for _, created, _ in rows)
    total_closed = sum(closed for _, _, closed in rows)
    if not rows or (total_created == 0 and total_closed == 0):
        return '<p class="panel-note">今日暂无工单入队或结案记录。</p>'

    max_val = max(max(created, closed) for _, created, closed in rows)
    y_max = max(2, max_val)
    if y_max % 2 != 0:
        y_max += 1
    y_mid = y_max // 2

    plot_left = 24.0
    plot_right = 408.0
    plot_top = 14.0
    plot_bottom = 80.0
    plot_height = plot_bottom - plot_top
    slot_w = (plot_right - plot_left) / len(rows)
    bar_w = min(13.0, max(4.0, (slot_w - 6.0) / 2.0))
    pair_gap = min(2.0, max(1.0, slot_w * 0.06))
    label_step = 1 if len(rows) <= 10 else 2

    bars: list[str] = []
    labels: list[str] = []
    peak_label = ""
    peak_created = 0
    for idx, (label, created, closed) in enumerate(rows):
        if created > peak_created:
            peak_created = created
            peak_label = label
        center_x = plot_left + slot_w * (idx + 0.5)
        x1 = center_x - pair_gap / 2.0 - bar_w
        x2 = center_x + pair_gap / 2.0
        if created > 0:
            h1 = max(2.0, (created / y_max) * plot_height)
            y1 = plot_bottom - h1
            bars.append(
                f'<rect class="svg-bar--secondary" x="{x1:.1f}" y="{y1:.1f}" '
                f'width="{bar_w:.1f}" height="{h1:.1f}" rx="1.5" />'
            )
        if closed > 0:
            h2 = max(2.0, (closed / y_max) * plot_height)
            y2 = plot_bottom - h2
            bars.append(
                f'<rect class="svg-bar--solid" x="{x2:.1f}" y="{y2:.1f}" '
                f'width="{bar_w:.1f}" height="{h2:.1f}" rx="1.5" />'
            )
        if idx % label_step == 0 or idx == len(rows) - 1:
            labels.append(
                f'<text class="svg-label" x="{center_x:.1f}" y="96" text-anchor="middle">{_esc(label)}</text>'
            )

    aria = f"每小时入队与已结数量对比，累计入队{total_created}项，已结{total_closed}项"
    peak_legend = (
        f'<span class="legend-item"><span class="swatch c-ochre"></span>{_esc(peak_label)} 入队峰值 {peak_created}</span>'
        if peak_created > 0
        else ""
    )
    return (
        '<figure class="chart-figure">'
        f'<svg class="chart-svg" viewBox="0 0 420 104" role="img" aria-label="{_esc(aria)}">'
        '<line class="svg-grid" x1="24" y1="14" x2="408" y2="14" />'
        '<line class="svg-grid" x1="24" y1="47" x2="408" y2="47" />'
        '<line class="svg-axis" x1="24" y1="80" x2="408" y2="80" />'
        f'<text class="svg-label" x="18" y="17" text-anchor="end">{y_max}</text>'
        f'<text class="svg-label" x="18" y="50" text-anchor="end">{y_mid}</text>'
        '<text class="svg-label" x="18" y="83" text-anchor="end">0</text>'
        f'{"".join(bars)}{"".join(labels)}'
        "</svg>"
        '<div class="chart-legend">'
        f'<span class="legend-item"><span class="swatch c-muted"></span>入队 {total_created}</span>'
        f'<span class="legend-item"><span class="swatch c-green"></span>已结 {total_closed}</span>'
        f"{peak_legend}"
        "</div>"
        "</figure>"
    )


def _default_hbar(
    items: Sequence[tuple[str, int, str]],
    *,
    aria_label: str = "",
    show_stack_bar: bool = True,
) -> str:
    cleaned = [(str(label), max(0, _int(count)), str(color)) for label, count, color in items]
    total = sum(count for _, count, _ in cleaned)
    if total == 0:
        return '<p class="panel-note">暂无已结耗时分布数据。</p>'

    max_count = max(count for _, count, _ in cleaned)
    stack_html = ""
    if show_stack_bar:
        segs = [
            f'<span class="{_esc(color)}" style="--w: {(count / total) * 100:.1f}%"></span>'
            for _, count, color in cleaned
            if count > 0
        ]
        stack_html = f'<div class="stack-bar" aria-hidden="true">{"".join(segs)}</div>'

    rows: list[str] = []
    for label, count, color in cleaned:
        pct = round((count / total) * 100)
        bar_w = round((count / max_count) * 100) if max_count > 0 else 0
        fill_cls = "bar-fill" if color == "c-green" else f"bar-fill {_esc(color)}"
        rows.append(
            '<li class="hbar-row">'
            f'<span class="hbar-label"><span class="swatch {_esc(color)}"></span>{_esc(label)}</span>'
            f'<span class="bar-track" aria-hidden="true"><span class="{fill_cls}" style="--bar-size: {bar_w}%"></span></span>'
            f'<span class="hbar-val">{count}<span>{pct}%</span></span>'
            "</li>"
        )
    aria_attr = f' aria-label="{_esc(aria_label)}"' if aria_label else ""
    return f'{stack_html}<ol class="hbar-list"{aria_attr}>{"".join(rows)}</ol>'


def _render_view_tabs(
    *,
    active_view: str,
    open_total: int,
    feedback_count: int,
    inbox_count: int | None,
) -> str:
    tabs: list[tuple[str, str, str, int | None]] = [
        ("all", "/dashboard/librarian", "全部视图", None),
        ("cases", "/dashboard/librarian?view=cases", "待办工单", open_total),
        ("inbox", "/dashboard/librarian?view=inbox", "读者私信", inbox_count),
        ("feedback", "/dashboard/librarian?view=feedback", "意见反馈", feedback_count),
    ]
    links: list[str] = []
    for key, href, label, count in tabs:
        current = ' aria-current="page"' if active_view == key else ""
        count_html = f' <span class="view-tab-count">{count}</span>' if count is not None else ""
        links.append(f'<a href="{href}"{current}>{_esc(label)}{count_html}</a>')
    return f'<nav class="view-tabs" aria-label="视图筛选">{"".join(links)}</nav>'


def _render_case_thread(case: Mapping[str, Any]) -> str:
    raw_messages = case.get("messages")
    messages = raw_messages if isinstance(raw_messages, Sequence) and not isinstance(raw_messages, (str, bytes)) else ()
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        role_raw = str(msg.get("sender_role") or "reader").strip().lower()
        role_label = _SENDER_ROLE_LABELS.get(role_raw, role_raw or "读者")
        hm = _extract_hm(msg.get("created_hm") or msg.get("created_at_utc8"))
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        prefix = f"{role_label} {hm}" if hm else role_label
        lines.append(f'<div class="queue-thread"><strong>{_esc(prefix)}：</strong>{_esc(content)}</div>')

    if not lines:
        fallback_text = str(case.get("latest_message") or case.get("summary") or "").strip()
        if fallback_text:
            hm = _extract_hm(case.get("created_hm") or case.get("created_at_utc8"))
            prefix = f"读者 {hm}" if hm else "读者"
            lines.append(f'<div class="queue-thread"><strong>{_esc(prefix)}：</strong>{_esc(fallback_text)}</div>')
        else:
            lines.append('<div class="queue-thread">暂无留言记录。</div>')
    return "".join(lines)


def _render_close_form(case_id: object, *, active_view: str) -> str:
    safe_id = _safe_case_id(case_id)
    return (
        f'<form class="case-close-form" method="post" action="/dashboard/librarian/cases/{safe_id}/close">'
        f'<input type="hidden" name="view" value="{_esc(active_view)}">'
        '<input type="text" name="resolution_note" class="form-control" maxlength="500" '
        'placeholder="填写处理结果或备注（选填）">'
        '<label class="confirm-label"><input type="checkbox" name="confirm" value="yes" required> '
        "确认已在公众号后台回复/处置</label>"
        '<button class="button button--quiet" type="submit">标记结案</button>'
        "</form>"
    )


def _render_metric_strip(
    *,
    open_total: int,
    open_emg: int,
    open_todo_count: int,
    longest_wait: Mapping[str, Any],
    today_stats: Mapping[str, Any],
    response_stats: Mapping[str, Any],
    micro_bar_fn: Callable[[Sequence[tuple[str, float]]], str],
    sparkline_fn: Callable[[Sequence[int | float]], str],
) -> str:
    # 1. 待跟进
    if open_emg > 0:
        open_delta = f'<span class="metric-delta metric-delta--warn">{open_emg} 紧急</span>'
    else:
        open_delta = '<span class="metric-delta">无紧急</span>'
    if open_total > 0:
        emg_pct = (open_emg / open_total) * 100.0
        todo_pct = (open_todo_count / open_total) * 100.0
        open_bar = micro_bar_fn([("c-rust", emg_pct), ("c-ink", todo_pct)])
    else:
        open_bar = micro_bar_fn([])

    # 2. 最长等待
    wait_min = max(0, _int(longest_wait.get("minutes")))
    wait_hm = _extract_hm(longest_wait.get("created_hm"))
    wait_summary = _truncate(str(longest_wait.get("summary") or ""), limit=24)
    if open_total == 0 and wait_min == 0 and not wait_summary:
        wait_delta = '<span class="metric-delta">队列已清</span>'
        wait_bar = micro_bar_fn([])
        wait_note = "当前无待跟进工单"
    else:
        if wait_min >= 120:
            wait_delta = f'<span class="metric-delta metric-delta--warn">超 {wait_min // 60}h</span>'
            wait_bar_color = "c-rust"
        elif wait_min >= 60:
            wait_delta = '<span class="metric-delta metric-delta--warn">超 1h</span>'
            wait_bar_color = "c-rust"
        elif wait_min >= 30:
            wait_delta = '<span class="metric-delta">等待中</span>'
            wait_bar_color = "c-ochre"
        else:
            wait_delta = '<span class="metric-delta">正常</span>'
            wait_bar_color = "c-green"
        wait_pct = min(100.0, max(5.0, (wait_min / 120.0) * 100.0)) if wait_min > 0 else 0.0
        wait_bar = micro_bar_fn([(wait_bar_color, wait_pct)]) if wait_pct > 0 else micro_bar_fn([])
        if wait_hm and wait_summary:
            wait_note = f"{wait_hm} · {wait_summary}"
        elif wait_summary:
            wait_note = wait_summary
        elif wait_hm:
            wait_note = f"入队 {wait_hm}"
        else:
            wait_note = "当前最长等待"

    # 3. 今日已结
    today_created = max(0, _int(today_stats.get("created")))
    today_closed = max(0, _int(today_stats.get("closed")))
    raw_rate = _float_or_none(today_stats.get("close_rate_pct"))
    if raw_rate is None:
        close_rate = (today_closed / today_created) * 100.0 if today_created > 0 else 0.0
    else:
        close_rate = max(0.0, min(100.0, raw_rate))
    today_delta = f'<span class="metric-delta">{close_rate:.1f}%</span>' if today_created > 0 else '<span class="metric-delta">—</span>'
    today_bar = micro_bar_fn([("c-green", close_rate)]) if (today_created > 0 and today_closed > 0) else micro_bar_fn([])

    # 4. 均响耗时
    avg_min = _float_or_none(response_stats.get("avg_minutes"))
    raw_durations = response_stats.get("recent_durations")
    durations = (
        list(raw_durations)
        if isinstance(raw_durations, Sequence) and not isinstance(raw_durations, (str, bytes))
        else []
    )
    sample_count = max(0, _int(response_stats.get("sample_count"), default=len(durations)))
    scope = str(response_stats.get("scope") or "recent").strip().lower()
    spark_html = sparkline_fn(durations)
    if avg_min is None:
        resp_delta = '<span class="metric-delta">暂无样本</span>'
        resp_value = '<p class="metric-value">—</p>'
        resp_note = "暂无已结样本"
    else:
        if avg_min <= 30.0:
            resp_delta = '<span class="metric-delta">达标 ≤30m</span>'
        else:
            resp_delta = '<span class="metric-delta metric-delta--warn">超 30m</span>'
        resp_value = f'<p class="metric-value">{_esc(_format_avg_minutes(avg_min))}<span class="metric-unit">分</span></p>'
        if scope == "today":
            resp_note = f"今日 {sample_count} 笔已结均值"
        else:
            resp_note = f"近 {sample_count} 笔已结均值"

    return (
        '<section class="metric-strip" aria-label="当前队列概览">'
        '<article class="metric">'
        f'<div class="metric-top"><h2 class="metric-label">待跟进</h2>{open_delta}</div>'
        f'<div class="metric-mid"><p class="metric-value">{open_total}<span class="metric-unit">项</span></p></div>'
        f"{open_bar}"
        f'<p class="metric-note">紧急 {open_emg} · 普通 {open_todo_count}</p>'
        "</article>"
        '<article class="metric">'
        f'<div class="metric-top"><h2 class="metric-label">最长等待</h2>{wait_delta}</div>'
        f'<div class="metric-mid"><p class="metric-value">{wait_min}<span class="metric-unit">分</span></p></div>'
        f"{wait_bar}"
        f'<p class="metric-note">{_esc(wait_note)}</p>'
        "</article>"
        '<article class="metric">'
        f'<div class="metric-top"><h2 class="metric-label">今日已结</h2>{today_delta}</div>'
        f'<div class="metric-mid"><p class="metric-value">{today_closed}<span class="metric-unit">项</span></p></div>'
        f"{today_bar}"
        f'<p class="metric-note">今日累计入队 {today_created} 项</p>'
        "</article>"
        '<article class="metric">'
        f'<div class="metric-top"><h2 class="metric-label">均响耗时</h2>{resp_delta}</div>'
        f'<div class="metric-mid">{resp_value}{spark_html}</div>'
        f'<p class="metric-note">{_esc(resp_note)}</p>'
        "</article>"
        "</section>"
    )


def _render_emergency_banners(
    open_emergency: Sequence[Mapping[str, Any]],
    *,
    active_view: str,
) -> str:
    banners: list[str] = []
    for idx, case in enumerate(open_emergency):
        summary = _truncate(str(case.get("summary") or case.get("latest_message") or "紧急事项"), limit=60)
        created_hm = _extract_hm(case.get("created_hm") or case.get("created_at_utc8"))
        wait_min = max(0, _int(case.get("wait_minutes")))
        desc_parts: list[str] = []
        if created_hm:
            desc_parts.append(created_hm)
        desc_parts.append(f"已等待 {wait_min} 分")
        heading_id = f"priority-title-{idx + 1}" if idx > 0 else "priority-title"
        thread_html = _render_case_thread(case)
        form_html = _render_close_form(case.get("case_id", ""), active_view=active_view)
        banners.append(
            f'<section class="urgent-banner" aria-labelledby="{heading_id}">'
            '<div class="urgent-banner-main">'
            '<span class="urgent-dot" aria-hidden="true"></span>'
            '<span class="badge urgent">紧急</span>'
            f'<h2 id="{heading_id}">{_esc(summary)}</h2>'
            f'<span class="urgent-banner-desc">{_esc(" · ".join(desc_parts))}</span>'
            "</div>"
            '<div class="urgent-banner-meta"><span>已发紧急联系方式</span></div>'
            '<details class="queue-details">'
            "<summary>展开留言与结案</summary>"
            f"{thread_html}{form_html}"
            "</details>"
            "</section>"
        )
    return "".join(banners)


def _render_open_todo_panel(
    open_todo: Sequence[Mapping[str, Any]],
    *,
    active_view: str,
) -> str:
    count = len(open_todo)
    header = (
        '<div class="panel-head">'
        '<h2 id="queue-title">普通未结案队列</h2>'
        f'<span class="period-chip">按等待时长降序 · {count} 条</span>'
        "</div>"
    )
    if not open_todo:
        return (
            '<section class="panel panel--span-8 panel--queue-main" aria-labelledby="queue-title">'
            f"{header}"
            '<p class="panel-note">当前没有未结普通待办。</p>'
            "</section>"
        )

    max_wait = max((max(0, _int(case.get("wait_minutes"))) for case in open_todo), default=0)
    rows: list[str] = []
    for case in open_todo:
        summary = _truncate(str(case.get("summary") or case.get("latest_message") or "未命名待办"), limit=48)
        raw_kind = str(case.get("case_kind") or "staff_todo").strip().lower()
        kind_label = _CASE_KIND_LABELS.get(raw_kind, raw_kind or "待办")
        created_hm = _extract_hm(case.get("created_hm") or case.get("created_at_utc8"))
        wait_min = max(0, _int(case.get("wait_minutes")))
        bar_pct = max(6, round((wait_min / max_wait) * 100)) if max_wait > 0 and wait_min > 0 else 0

        if wait_min >= 60:
            wait_cls = "wait-label wait-label--high"
            bar_cls = "bar-fill c-rust"
            priority_badge = '<span class="badge urgent queue-action">优先</span>'
        elif wait_min >= 30:
            wait_cls = "wait-label wait-label--mid"
            bar_cls = "bar-fill c-ochre"
            priority_badge = '<span class="badge ochre queue-action">待跟进</span>'
        elif wait_min >= 15:
            wait_cls = "wait-label"
            bar_cls = "bar-fill c-teal"
            priority_badge = '<span class="badge muted queue-action">待跟进</span>'
        else:
            wait_cls = "wait-label"
            bar_cls = "bar-fill c-green"
            priority_badge = '<span class="badge muted queue-action">待跟进</span>'

        time_html = f'<span class="queue-time">{_esc(created_hm)}</span>' if created_hm else ""
        thread_html = _render_case_thread(case)
        form_html = _render_close_form(case.get("case_id", ""), active_view=active_view)
        rows.append(
            '<li class="queue-row">'
            '<div class="queue-title-line">'
            f"<h3>{_esc(summary)}</h3>"
            f'<span class="badge muted">{_esc(kind_label)}</span>'
            f"{time_html}"
            "</div>"
            '<div class="wait-cell">'
            f'<div class="{wait_cls}"><span>等待</span><strong>{wait_min}分</strong></div>'
            f'<span class="bar-track" aria-hidden="true"><span class="{bar_cls}" style="--bar-size: {bar_pct}%"></span></span>'
            "</div>"
            f"{priority_badge}"
            '<details class="queue-details">'
            "<summary>展开留言与结案</summary>"
            f"{thread_html}{form_html}"
            "</details>"
            "</li>"
        )

    return (
        '<section class="panel panel--span-8 panel--queue-main" aria-labelledby="queue-title">'
        f'{header}<ol class="queue-list">{"".join(rows)}</ol>'
        "</section>"
    )


def _render_right_stack(
    *,
    open_total: int,
    wait_buckets: Mapping[str, Any],
    recent_closed: Sequence[Mapping[str, Any]],
    donut_fn: Callable[..., str],
) -> str:
    emg = max(0, _int(wait_buckets.get("emergency")))
    over_60 = max(0, _int(wait_buckets.get("over_60m")))
    m30_60 = max(0, _int(wait_buckets.get("m30_60")))
    under_30 = max(0, _int(wait_buckets.get("under_30m")))
    donut_segments = [
        ("紧急事项", emg, "c-rust"),
        ("等待 ≥60m", over_60, "c-ochre"),
        ("30–60m", m30_60, "c-teal"),
        ("<30m", under_30, "c-green"),
    ]
    donut_aria = (
        f"{open_total}项待跟进分布：紧急{emg}项，等待60分钟以上{over_60}项，"
        f"30至60分钟{m30_60}项，30分钟以内{under_30}项"
    )
    donut_html = donut_fn(
        donut_segments,
        center_value=str(open_total),
        center_label="待跟进",
        aria_label=donut_aria,
    )
    wait_panel = (
        '<section class="panel" aria-labelledby="wait-dist-title">'
        '<div class="panel-head">'
        '<h2 id="wait-dist-title">待办结构</h2>'
        f'<span class="period-chip">{open_total} 项未结</span>'
        "</div>"
        f"{donut_html}"
        "</section>"
    )

    shown_closed = list(recent_closed[:6])
    if not shown_closed:
        closed_body = '<p class="panel-note">暂无已结工单</p>'
    else:
        items: list[str] = []
        for item in shown_closed:
            closed_hm = _extract_hm(item.get("closed_hm") or item.get("closed_at_utc8")) or "--:--"
            summary = _truncate(str(item.get("summary") or "已结工单"), limit=32)
            note = _truncate(str(item.get("resolution_note") or ""), limit=40)
            dur = _float_or_none(item.get("duration_minutes"))
            dur_badge = f"{int(round(dur))}分" if dur is not None else "已结"
            text = f"{summary} · {note}" if note else summary
            items.append(
                '<li class="alert-row">'
                f"<time>{_esc(closed_hm)}</time>"
                f'<span class="badge good">{_esc(dur_badge)}</span>'
                f'<span class="alert-text">{_esc(text)}</span>'
                "</li>"
            )
        closed_body = f'<ul class="alert-list">{"".join(items)}</ul>'

    closed_panel = (
        '<section class="panel" aria-labelledby="closed-title">'
        '<div class="panel-head">'
        '<h2 id="closed-title">最近已结工单</h2>'
        f'<span class="badge good">{len(recent_closed)} 条</span>'
        "</div>"
        f"{closed_body}"
        "</section>"
    )
    return f'<div class="right-stack">{wait_panel}{closed_panel}</div>'


def _render_charts_row(
    *,
    hourly_pace: Sequence[Mapping[str, Any]],
    today_stats: Mapping[str, Any],
    response_stats: Mapping[str, Any],
    latency_buckets: Mapping[str, Any],
    pace_fn: Callable[[Sequence[Mapping[str, Any]]], str],
    hbar_fn: Callable[..., str],
) -> str:
    today_created = max(0, _int(today_stats.get("created")))
    today_closed = max(0, _int(today_stats.get("closed")))
    if hourly_pace:
        first_h = _int(hourly_pace[0].get("hour"))
        last_h = _int(hourly_pace[-1].get("hour"))
        pace_chip = f"{first_h:02d}–{last_h:02d}h · {today_created} 入 · {today_closed} 结"
    else:
        pace_chip = f"今日 {today_created} 入 · {today_closed} 结"

    pace_panel = (
        '<section class="panel panel--span-7" aria-labelledby="pace-title">'
        '<div class="panel-head">'
        '<h2 id="pace-title">今日入队 vs 已结</h2>'
        f'<span class="period-chip">{_esc(pace_chip)}</span>'
        "</div>"
        f"{pace_fn(hourly_pace)}"
        "</section>"
    )

    le_10m = max(0, _int(latency_buckets.get("le_10m")))
    m10_20 = max(0, _int(latency_buckets.get("m10_20")))
    m20_30 = max(0, _int(latency_buckets.get("m20_30")))
    gt_30m = max(0, _int(latency_buckets.get("gt_30m")))
    bucket_total = max(0, _int(latency_buckets.get("total"), default=le_10m + m10_20 + m20_30 + gt_30m))
    within_raw = _float_or_none(latency_buckets.get("within_30m_pct"))
    if within_raw is None:
        within_pct = round(((le_10m + m10_20 + m20_30) / bucket_total) * 100) if bucket_total > 0 else 0
    else:
        within_pct = round(within_raw)

    avg_min = _float_or_none(response_stats.get("avg_minutes"))
    if avg_min is not None:
        avg_badge = f'<span class="badge good">均 {_esc(_format_avg_minutes(avg_min))}m</span>'
    else:
        avg_badge = '<span class="badge muted">暂无样本</span>'

    latency_items = [
        ("≤10 分钟", le_10m, "c-green"),
        ("10–20 分", m10_20, "c-teal"),
        ("20–30 分", m20_30, "c-ochre"),
        (">30 分钟", gt_30m, "c-rust"),
    ]
    latency_aria = (
        f"已结案响应耗时：10分钟内{le_10m}项，10至20分钟{m10_20}项，"
        f"20至30分钟{m20_30}项，30分钟以上{gt_30m}项"
    )
    hbar_html = hbar_fn(latency_items, aria_label=latency_aria, show_stack_bar=True)
    footnote = (
        f"{within_pct}% 在 30 分钟目标内结案" if bucket_total > 0 else "暂无已结工单耗时记录"
    )
    latency_panel = (
        '<section class="panel panel--span-5" aria-labelledby="latency-title">'
        '<div class="panel-head">'
        '<h2 id="latency-title">已结响应耗时分布</h2>'
        f"{avg_badge}"
        "</div>"
        f"{hbar_html}"
        f'<p class="panel-footnote">{_esc(footnote)}</p>'
        "</section>"
    )
    return f"{pace_panel}{latency_panel}"


def _render_feedback_panel(recent_feedbacks: Sequence[Mapping[str, Any]]) -> str:
    count = len(recent_feedbacks)
    header = (
        '<div class="panel-head">'
        '<h2 id="feedback-title">意见反馈</h2>'
        f'<span class="period-chip">{count} 条</span>'
        "</div>"
    )
    if not recent_feedbacks:
        return (
            '<section id="feedback" class="panel panel--span-12" aria-labelledby="feedback-title">'
            f"{header}"
            '<p class="panel-note">暂无意见反馈。</p>'
            "</section>"
        )

    rows: list[str] = []
    for fb in recent_feedbacks:
        created_at = str(fb.get("created_at_utc8") or "").strip()
        content = str(fb.get("content") or "").strip() or "（空内容）"
        raw_status = str(fb.get("status") or "open").strip().lower()
        status_label, badge_kind = _FEEDBACK_STATUS_LABELS.get(raw_status, (raw_status or "已记录", "muted"))
        staff_note = str(fb.get("staff_note") or "").strip()
        note_html = f'<p class="case-meta"><span>馆员备注：{_esc(staff_note)}</span></p>' if staff_note else ""
        meta_html = f'<p class="case-meta"><span>{_esc(created_at)}</span></p>' if created_at else ""
        rows.append(
            '<li class="case-row">'
            '<div class="case-main">'
            f"{meta_html}"
            f'<p class="thread">{_esc(content)}</p>'
            f"{note_html}"
            "</div>"
            f'<span class="badge {_esc(badge_kind)} case-state">{_esc(status_label)}</span>'
            "</li>"
        )
    return (
        '<section id="feedback" class="panel panel--span-12" aria-labelledby="feedback-title">'
        f'{header}<ul class="case-list">{"".join(rows)}</ul>'
        "</section>"
    )


def render_librarian_workbench(
    analytics: Mapping[str, Any],
    *,
    inbox_html: str,
    active_view: str = "all",
    notice: str = "",
    render_sparkline_fn: Callable[[Sequence[int | float]], str] | None = None,
    render_donut_fn: Callable[..., str] | None = None,
    render_pace_fn: Callable[[Sequence[Mapping[str, Any]]], str] | None = None,
    render_hbar_fn: Callable[..., str] | None = None,
    render_micro_bar_fn: Callable[[Sequence[tuple[str, float]]], str] | None = None,
) -> str:
    """Render the full Librarian Workbench body HTML from analytics and retained inbox HTML.

    Optional chart renderer callables override built-in safe SVG/HTML renderers:
    - ``render_micro_bar_fn(segments: Sequence[tuple[str, float]]) -> str``
    - ``render_sparkline_fn(values: Sequence[int | float]) -> str``
    - ``render_donut_fn(segments, *, center_value, center_label, aria_label="") -> str``
    - ``render_pace_fn(hourly_pace: Sequence[Mapping[str, Any]]) -> str``
    - ``render_hbar_fn(items, *, aria_label="", show_stack_bar=True) -> str``
    """
    view = active_view if active_view in _VALID_VIEWS else "all"
    data = analytics if isinstance(analytics, Mapping) else {}

    raw_emergency = data.get("open_emergency")
    open_emergency = (
        [item for item in raw_emergency if isinstance(item, Mapping)]
        if isinstance(raw_emergency, Sequence) and not isinstance(raw_emergency, (str, bytes))
        else []
    )
    raw_todo = data.get("open_todo")
    open_todo = (
        [item for item in raw_todo if isinstance(item, Mapping)]
        if isinstance(raw_todo, Sequence) and not isinstance(raw_todo, (str, bytes))
        else []
    )

    open_counts = data.get("open_counts") if isinstance(data.get("open_counts"), Mapping) else {}
    open_emg = max(0, _int(open_counts.get("emergency"), default=len(open_emergency)))
    open_todo_count = max(0, _int(open_counts.get("staff_todo"), default=len(open_todo)))
    open_total = max(0, _int(open_counts.get("total"), default=open_emg + open_todo_count))

    wait_buckets = data.get("wait_buckets") if isinstance(data.get("wait_buckets"), Mapping) else {}
    longest_wait = data.get("longest_wait") if isinstance(data.get("longest_wait"), Mapping) else {}
    today_stats = data.get("today_stats") if isinstance(data.get("today_stats"), Mapping) else {}
    response_stats = data.get("response_stats") if isinstance(data.get("response_stats"), Mapping) else {}
    latency_buckets = data.get("latency_buckets") if isinstance(data.get("latency_buckets"), Mapping) else {}

    raw_pace = data.get("hourly_pace_today")
    hourly_pace = (
        [item for item in raw_pace if isinstance(item, Mapping)]
        if isinstance(raw_pace, Sequence) and not isinstance(raw_pace, (str, bytes))
        else []
    )
    raw_closed = data.get("recent_closed")
    recent_closed = (
        [item for item in raw_closed if isinstance(item, Mapping)]
        if isinstance(raw_closed, Sequence) and not isinstance(raw_closed, (str, bytes))
        else []
    )
    raw_feedbacks = data.get("recent_feedbacks")
    recent_feedbacks = (
        [item for item in raw_feedbacks if isinstance(item, Mapping)]
        if isinstance(raw_feedbacks, Sequence) and not isinstance(raw_feedbacks, (str, bytes))
        else []
    )

    inbox_count: int | None
    if data.get("inbox_count") is not None:
        inbox_count = max(0, _int(data.get("inbox_count")))
    elif 'class="case-row"' in inbox_html:
        inbox_count = inbox_html.count('class="case-row"')
    else:
        inbox_count = None

    micro_bar_fn = render_micro_bar_fn or _default_micro_bar
    sparkline_fn = render_sparkline_fn or _default_sparkline
    donut_fn = render_donut_fn or _default_donut
    pace_fn = render_pace_fn or _default_pace_chart
    hbar_fn = render_hbar_fn or _default_hbar

    parts: list[str] = [
        _render_view_tabs(
            active_view=view,
            open_total=open_total,
            feedback_count=len(recent_feedbacks),
            inbox_count=inbox_count,
        )
    ]

    clean_notice = str(notice or "").strip()
    if clean_notice:
        parts.append(f'<p class="form-notice" role="status">{_esc(clean_notice)}</p>')

    parts.append(
        _render_metric_strip(
            open_total=open_total,
            open_emg=open_emg,
            open_todo_count=open_todo_count,
            longest_wait=longest_wait,
            today_stats=today_stats,
            response_stats=response_stats,
            micro_bar_fn=micro_bar_fn,
            sparkline_fn=sparkline_fn,
        )
    )

    show_cases = view in ("all", "cases")
    show_feedback = view in ("all", "feedback")
    show_inbox = view in ("all", "inbox")

    if show_cases and open_emergency:
        parts.append(_render_emergency_banners(open_emergency, active_view=view))

    grid_parts: list[str] = []
    if show_cases:
        grid_parts.append(_render_open_todo_panel(open_todo, active_view=view))
        grid_parts.append(
            _render_right_stack(
                open_total=open_total,
                wait_buckets=wait_buckets,
                recent_closed=recent_closed,
                donut_fn=donut_fn,
            )
        )
        grid_parts.append(
            _render_charts_row(
                hourly_pace=hourly_pace,
                today_stats=today_stats,
                response_stats=response_stats,
                latency_buckets=latency_buckets,
                pace_fn=pace_fn,
                hbar_fn=hbar_fn,
            )
        )
    if show_feedback:
        grid_parts.append(_render_feedback_panel(recent_feedbacks))
    if show_inbox:
        grid_parts.append(f'<div id="inbox" class="panel--span-12">{inbox_html}</div>')

    parts.append(f'<div class="dashboard-grid">{"".join(grid_parts)}</div>')
    return "".join(parts)
