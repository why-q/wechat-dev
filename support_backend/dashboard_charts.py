"""Pure inline SVG charts and admin overview renderers for the live dashboard.

Zero JavaScript, zero external dependencies, strict HTML/SVG escaping, and
complete zero/empty/extreme-value safety. Reuses the exact CSS classes from
``docs/dashboard-demo/dashboard.css``.
"""

from __future__ import annotations

import html
import math
from typing import Any, Mapping, Sequence


_SAFE_BADGE_CLASSES = frozenset({"good", "muted", "ochre", "urgent"})
_ALERT_KIND_DEFAULTS: dict[str, tuple[str, str]] = {
    "reply_failed": ("发信失败", "urgent"),
    "send_failed": ("发信失败", "urgent"),
    "failed": ("发信失败", "urgent"),
    "emergency": ("紧急事件", "urgent"),
    "staff_fallback": ("馆员兜底", "muted"),
    "fallback": ("馆员兜底", "muted"),
    "shadow": ("影子模式", "ochre"),
}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(int(value))
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _safe_int(value: object, default: int = 0) -> int:
    num = _safe_float(value, float(default))
    try:
        return int(round(num))
    except (ValueError, OverflowError):
        return default


def _nonneg_int(value: object) -> int:
    return max(0, _safe_int(value, 0))


def _nonneg_float(value: object) -> float:
    return max(0.0, _safe_float(value, 0.0))


def _fmt_count(value: int | float) -> str:
    num = _safe_int(value, 0)
    return f"{num:,}"


def _fmt_pct(part: float, total: float, *, decimals: int = 1) -> str:
    if total <= 0 or not math.isfinite(part) or not math.isfinite(total):
        return f"{0.0:.{decimals}f}%"
    pct = max(0.0, (part / total) * 100.0)
    return f"{pct:.{decimals}f}%"


def _fmt_num(value: float) -> str:
    """Format an SVG coordinate cleanly with at most one decimal place."""
    if not math.isfinite(value):
        return "0"
    rounded = round(value, 1)
    if rounded == 0.0:
        return "0"
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


def _nice_axis_max(max_val: float, *, intervals: int = 3) -> int:
    """Return a clean rounded integer Y-axis ceiling divisible by ``intervals``."""
    intervals = max(1, int(intervals))
    if not math.isfinite(max_val) or max_val <= 0:
        return intervals
    raw_step = max_val / intervals
    if raw_step <= 1.0:
        step = 1
    else:
        magnitude = 10 ** int(math.floor(math.log10(raw_step)))
        normalized = raw_step / magnitude
        if normalized <= 1.0:
            mult = 1
        elif normalized <= 2.0:
            mult = 2
        elif normalized <= 5.0:
            mult = 5
        else:
            mult = 10
        step = int(mult * magnitude)
    top = step * intervals
    while top < max_val:
        top += step
    return max(intervals, top)


def _proportional_widths(counts: Sequence[float], *, min_pct: float = 2.0) -> list[float]:
    """Compute percentage widths summing to 100%, keeping tiny positive slices visible."""
    cleaned = [max(0.0, _safe_float(c, 0.0)) for c in counts]
    total = sum(cleaned)
    if total <= 0:
        return [0.0 for _ in cleaned]

    pos_indices = [idx for idx, val in enumerate(cleaned) if val > 0]
    if len(pos_indices) == 1:
        widths = [0.0 for _ in cleaned]
        widths[pos_indices[0]] = 100.0
        return widths

    effective_min = min(min_pct, 100.0 / len(pos_indices))
    widths = [(val / total) * 100.0 for val in cleaned]

    small = [idx for idx in pos_indices if widths[idx] < effective_min]
    if not small:
        return widths

    large = [idx for idx in pos_indices if widths[idx] >= effective_min]
    reserved = effective_min * len(small)
    remaining = max(0.0, 100.0 - reserved)
    large_sum = sum(widths[idx] for idx in large)

    adjusted = [0.0 for _ in cleaned]
    for idx in small:
        adjusted[idx] = effective_min
    if large and large_sum > 0:
        for idx in large:
            adjusted[idx] = (widths[idx] / large_sum) * remaining
    elif large:
        share = remaining / len(large)
        for idx in large:
            adjusted[idx] = share
    return adjusted


def _render_stack_bar_html(segments: Sequence[tuple[str, int | float]]) -> str:
    """Render a `.stack-bar` track with proportional color segments."""
    counts = [_nonneg_float(count) for _, count in segments]
    if sum(counts) <= 0:
        return '<div class="stack-bar" aria-hidden="true"></div>'
    widths = _proportional_widths(counts, min_pct=1.5)
    spans = [
        f'<span class="{_escape(color_cls)}" style="--w: {w:.1f}%"></span>'
        for (color_cls, _), count, w in zip(segments, counts, widths)
        if count > 0 and w > 0
    ]
    return f'<div class="stack-bar" aria-hidden="true">{"".join(spans)}</div>'


def render_sparkline(
    values: list[int | float],
    *,
    width: int = 64,
    height: int = 24,
) -> str:
    """Render an inline SVG sparkline matching `.metric-spark`."""
    w = max(16, _safe_int(width, 64))
    h = max(10, _safe_int(height, 24))
    pad_x = 2.0
    pad_top = 4.0
    baseline_y = float(h - 2)
    pad_bottom = max(pad_top + 1.0, float(h - 5))
    flat_y = max(pad_top, float(h - 4))

    cleaned = [_nonneg_float(v) for v in values]
    if not cleaned or max(cleaned) <= 0:
        return (
            f'<svg class="metric-spark" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<line class="svg-axis" x1="{_fmt_num(pad_x)}" y1="{_fmt_num(flat_y)}" '
            f'x2="{_fmt_num(w - pad_x)}" y2="{_fmt_num(flat_y)}" />'
            "</svg>"
        )

    if len(cleaned) == 1:
        mid_y = (pad_top + pad_bottom) / 2.0
        coords = [(pad_x, mid_y), (float(w) - pad_x, mid_y)]
    else:
        min_v = min(cleaned)
        max_v = max(cleaned)
        span = max_v - min_v
        step_x = (float(w) - 2.0 * pad_x) / (len(cleaned) - 1)
        coords = []
        for idx, val in enumerate(cleaned):
            x = pad_x + idx * step_x
            if span <= 0:
                y = (pad_top + pad_bottom) / 2.0
            else:
                ratio = (val - min_v) / span
                y = pad_bottom - ratio * (pad_bottom - pad_top)
            coords.append((x, y))

    line_points = " ".join(f"{_fmt_num(x)},{_fmt_num(y)}" for x, y in coords)
    first_x = coords[0][0]
    last_x, last_y = coords[-1]
    area_points = (
        f"{line_points} {_fmt_num(last_x)},{_fmt_num(baseline_y)} "
        f"{_fmt_num(first_x)},{_fmt_num(baseline_y)}"
    )
    return (
        f'<svg class="metric-spark" viewBox="0 0 {w} {h}" aria-hidden="true">'
        f'<polygon class="svg-area" points="{area_points}" />'
        f'<polyline class="svg-line" points="{line_points}" />'
        f'<circle cx="{_fmt_num(last_x)}" cy="{_fmt_num(last_y)}" r="2.3" fill="var(--ink)" />'
        "</svg>"
    )


def render_micro_bar(segments: list[tuple[str, int | float]]) -> str:
    """Render a proportional `.micro-bar` track; empty track when total is 0."""
    counts = [_nonneg_float(count) for _, count in segments]
    if sum(counts) <= 0:
        return '<div class="micro-bar" aria-hidden="true"></div>'
    widths = _proportional_widths(counts, min_pct=2.0)
    spans = [
        f'<span class="{_escape(color_cls)}" style="--w: {w:.1f}%"></span>'
        for (color_cls, _), count, w in zip(segments, counts, widths)
        if count > 0 and w > 0
    ]
    return f'<div class="micro-bar" aria-hidden="true">{"".join(spans)}</div>'


def render_donut(
    segments: list[tuple[str, str, str, int]],
    *,
    center_num: str,
    center_sub: str,
    aria_label: str,
    small: bool = False,
    stack: bool = False,
) -> str:
    """Render an inline SVG donut chart paired with `.donut-legend`."""
    if small:
        size = 100
        cx = cy = 50
        radius = 36
        stroke_width = 11
        svg_cls = "donut-svg donut-svg--sm"
        num_y = 49
        sub_y = 62
        base_font_size = 18
    else:
        size = 116
        cx = cy = 58
        radius = 42
        stroke_width = 13
        svg_cls = "donut-svg"
        num_y = 57
        sub_y = 71
        base_font_size = 20

    circumference = 2.0 * math.pi * radius
    counts = [_nonneg_int(count) for _, _, _, count in segments]
    total = sum(counts)

    arcs_html = ""
    if total > 0:
        positive_count = sum(1 for c in counts if c > 0)
        gap = 2.0 if positive_count >= 2 else 0.0
        widths = _proportional_widths([float(c) for c in counts], min_pct=2.0)
        offset = 0.0
        arc_parts: list[str] = []
        for (_, _, stroke_var, _), count, pct in zip(segments, counts, widths):
            if count <= 0:
                continue
            seg_len = (pct / 100.0) * circumference
            dash = max(1.5, seg_len - gap) if gap > 0 else circumference
            dash_offset_str = "0" if offset == 0.0 else f"-{offset:.2f}"
            arc_parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
                f'stroke="{_escape(stroke_var)}" stroke-width="{stroke_width}" '
                f'stroke-dasharray="{dash:.1f} {circumference:.2f}" '
                f'stroke-dashoffset="{dash_offset_str}" />'
            )
            offset += seg_len
        arcs_html = f'<g transform="rotate(-90 {cx} {cy})">{"".join(arc_parts)}</g>'

    num_text = str(center_num)
    if len(num_text) >= 6:
        font_attr = f' font-size="{max(12, base_font_size - 6)}"'
    elif len(num_text) >= 4:
        font_attr = f' font-size="{max(14, base_font_size - 3)}"'
    elif small:
        font_attr = f' font-size="{base_font_size}"'
    else:
        font_attr = ""

    svg_html = (
        f'<svg class="{svg_cls}" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="{_escape(aria_label)}">'
        f'<circle class="donut-track" cx="{cx}" cy="{cy}" r="{radius}" stroke-width="{stroke_width}" />'
        f"{arcs_html}"
        f'<text class="donut-center-num" x="{cx}" y="{num_y}"{font_attr}>{_escape(num_text)}</text>'
        f'<text class="donut-center-sub" x="{cx}" y="{sub_y}">{_escape(center_sub)}</text>'
        "</svg>"
    )

    if segments:
        legend_items = []
        for (name, swatch_cls, _, _), count in zip(segments, counts):
            pct_text = _fmt_pct(float(count), float(total), decimals=1)
            legend_items.append(
                "<li>"
                f'<span class="swatch {_escape(swatch_cls)}"></span>'
                f'<span class="donut-name">{_escape(name)}</span>'
                f'<span class="donut-val">{_fmt_count(count)}</span>'
                f'<span class="donut-pct">{pct_text}</span>'
                "</li>"
            )
        legend_html = f'<ul class="donut-legend">{"".join(legend_items)}</ul>'
    else:
        legend_html = '<p class="panel-note">暂无记录</p>'

    layout_cls = "donut-layout donut-layout--stack" if stack else "donut-layout"
    return f'<div class="{layout_cls}">{svg_html}{legend_html}</div>'


def render_stack_hbar_list(
    items: list[tuple[str, str, int]],
    *,
    denominator: int | None = None,
    wide_label: bool = False,
    show_stack_bar: bool = True,
    aria_label: str = "",
) -> str:
    """Render an optional `.stack-bar` followed by a proportional `.hbar-list`."""
    if not items:
        prefix = '<div class="stack-bar" aria-hidden="true"></div>' if show_stack_bar else ""
        return f'{prefix}<p class="panel-note">暂无记录</p>'

    cleaned = [(label, color_cls, _nonneg_int(count)) for label, color_cls, count in items]
    items_total = sum(count for _, _, count in cleaned)
    denom = _nonneg_int(denominator) if denominator is not None else items_total

    stack_html = ""
    if show_stack_bar:
        stack_html = _render_stack_bar_html([(color_cls, count) for _, color_cls, count in cleaned])

    row_cls = "hbar-row hbar-row--wide-label" if wide_label else "hbar-row"
    rows_html: list[str] = []
    for label, color_cls, count in cleaned:
        escaped_cls = _escape(color_cls)
        if denom > 0 and count > 0:
            raw_pct = (count / denom) * 100.0
            bar_pct = min(100.0, max(2.0, raw_pct))
            pct_text = f"{raw_pct:.1f}%"
        else:
            bar_pct = 0.0
            pct_text = "0.0%"
        fill_cls = "bar-fill" if color_cls == "c-green" else f"bar-fill {escaped_cls}"
        rows_html.append(
            f'<li class="{row_cls}">'
            f'<span class="hbar-label"><span class="swatch {escaped_cls}"></span>{_escape(label)}</span>'
            f'<span class="bar-track" aria-hidden="true"><span class="{fill_cls}" style="--bar-size: {bar_pct:.1f}%"></span></span>'
            f'<span class="hbar-val">{_fmt_count(count)}<span>{pct_text}</span></span>'
            "</li>"
        )

    aria_attr = f' aria-label="{_escape(aria_label)}"' if aria_label else ""
    return f'{stack_html}<ol class="hbar-list"{aria_attr}>{"".join(rows_html)}</ol>'


def _extract_day_display(date_str: str, fallback_label: str) -> str:
    parts = date_str.split("-")
    if len(parts) == 3 and parts[2].isdigit():
        return f"{int(parts[2])}日"
    return fallback_label or date_str


def render_daily_trend_chart(daily_inbound: list[dict[str, Any]], *, avg: float) -> str:
    """Render the 7-day inbound trend SVG (`viewBox="0 0 360 148"`) and legend."""
    plot_top = 18.0
    plot_bottom = 120.0
    plot_height = plot_bottom - plot_top
    plot_right = 348.0
    avg_val = _nonneg_float(avg)

    rows: list[tuple[str, str, int]] = []
    for item in daily_inbound:
        if not isinstance(item, Mapping):
            continue
        date_str = str(item.get("date", ""))
        label = str(item.get("label", "")) or date_str[-2:]
        count = _nonneg_int(item.get("count", 0))
        rows.append((date_str, label, count))

    if not rows:
        svg = (
            '<svg class="chart-svg" viewBox="0 0 360 148" role="img" aria-label="近7日暂无入站记录">'
            '<line class="svg-grid" x1="28" y1="18" x2="348" y2="18" />'
            '<line class="svg-grid" x1="28" y1="52" x2="348" y2="52" />'
            '<line class="svg-grid" x1="28" y1="86" x2="348" y2="86" />'
            '<line class="svg-axis" x1="28" y1="120" x2="348" y2="120" />'
            '<text class="svg-label" x="22" y="21" text-anchor="end">3</text>'
            '<text class="svg-label" x="22" y="55" text-anchor="end">2</text>'
            '<text class="svg-label" x="22" y="89" text-anchor="end">1</text>'
            '<text class="svg-label" x="22" y="123" text-anchor="end">0</text>'
            '<text class="svg-label" x="188" y="72" text-anchor="middle">暂无记录</text>'
            "</svg>"
        )
        legend = (
            '<div class="chart-legend">'
            '<span class="legend-item"><span class="swatch c-green"></span>每日入站</span>'
            '<span class="legend-item"><span class="swatch swatch--line"></span>7日均值 0.0</span>'
            "</div>"
        )
        return f'<figure class="chart-figure">{svg}{legend}</figure>'

    counts = [c for _, _, c in rows]
    max_count = max(counts)
    y_max = _nice_axis_max(max(float(max_count), avg_val), intervals=3)
    tick_values = [y_max, (y_max * 2) // 3, y_max // 3, 0]
    tick_ys = [18.0, 52.0, 86.0, 120.0]

    max_tick_len = max(len(_fmt_count(t)) for t in tick_values)
    plot_left = max(28.0, 8.0 + max_tick_len * 6.0)
    tick_x = plot_left - 6.0

    elements: list[str] = []
    for y in tick_ys[:3]:
        elements.append(
            f'<line class="svg-grid" x1="{_fmt_num(plot_left)}" y1="{_fmt_num(y)}" '
            f'x2="{_fmt_num(plot_right)}" y2="{_fmt_num(y)}" />'
        )
    elements.append(
        f'<line class="svg-axis" x1="{_fmt_num(plot_left)}" y1="{_fmt_num(plot_bottom)}" '
        f'x2="{_fmt_num(plot_right)}" y2="{_fmt_num(plot_bottom)}" />'
    )

    if avg_val > 0 and max_count > 0:
        avg_y = max(plot_top, min(plot_bottom, plot_bottom - (avg_val / y_max) * plot_height))
        elements.append(
            f'<line class="svg-avg" x1="{_fmt_num(plot_left)}" y1="{_fmt_num(avg_y)}" '
            f'x2="{_fmt_num(plot_right)}" y2="{_fmt_num(avg_y)}" />'
        )

    for tick_val, y in zip(tick_values, tick_ys):
        elements.append(
            f'<text class="svg-label" x="{_fmt_num(tick_x)}" y="{_fmt_num(y + 3.0)}" '
            f'text-anchor="end">{_fmt_count(tick_val)}</text>'
        )

    n = len(rows)
    slot_w = (plot_right - plot_left) / n
    bar_w = min(20.0, max(6.0, slot_w * 0.44))
    peak_idx = counts.index(max_count) if max_count > 0 else -1

    points: list[tuple[float, float]] = []
    bars: list[str] = []
    dots: list[str] = []
    val_labels: list[str] = []
    x_labels: list[str] = []

    for idx, (date_str, label, count) in enumerate(rows):
        cx = plot_left + slot_w * (idx + 0.5)
        bar_h = (count / y_max) * plot_height if y_max > 0 else 0.0
        y = plot_bottom - bar_h
        points.append((cx, y))
        is_peak = idx == peak_idx

        if count > 0:
            bar_x = cx - bar_w / 2.0
            if is_peak:
                bars.append(
                    f'<rect class="svg-bar--peak" x="{_fmt_num(bar_x)}" y="{_fmt_num(y)}" '
                    f'width="{_fmt_num(bar_w)}" height="{_fmt_num(bar_h)}" rx="1.5" opacity="0.24" />'
                )
            else:
                bars.append(
                    f'<rect class="svg-bar" x="{_fmt_num(bar_x)}" y="{_fmt_num(y)}" '
                    f'width="{_fmt_num(bar_w)}" height="{_fmt_num(bar_h)}" rx="1.5" />'
                )

        if is_peak:
            dots.append(f'<circle class="svg-dot--peak" cx="{_fmt_num(cx)}" cy="{_fmt_num(y)}" r="3.6" />')
            val_cls = "svg-val svg-val--peak"
            val_y = max(11.0, y - 7.5)
        else:
            dots.append(f'<circle class="svg-dot" cx="{_fmt_num(cx)}" cy="{_fmt_num(y)}" r="2.8" />')
            val_cls = "svg-val"
            val_y = max(11.0, y - 7.0)

        val_labels.append(
            f'<text class="{val_cls}" x="{_fmt_num(cx)}" y="{_fmt_num(val_y)}" '
            f'text-anchor="middle">{_fmt_count(count)}</text>'
        )
        x_labels.append(
            f'<text class="svg-label" x="{_fmt_num(cx)}" y="136" '
            f'text-anchor="middle">{_escape(label)}</text>'
        )

    elements.extend(bars)

    if max_count > 0:
        if len(points) >= 2:
            line_pts = " ".join(f"{_fmt_num(x)},{_fmt_num(y)}" for x, y in points)
            area_pts = (
                f"{line_pts} {_fmt_num(points[-1][0])},{_fmt_num(plot_bottom)} "
                f"{_fmt_num(points[0][0])},{_fmt_num(plot_bottom)}"
            )
            elements.append(f'<polygon class="svg-area" points="{area_pts}" />')
            elements.append(f'<polyline class="svg-line" points="{line_pts}" />')
        elements.extend(dots)
        elements.extend(val_labels)
    else:
        if len(points) >= 2:
            flat_pts = " ".join(f"{_fmt_num(x)},{_fmt_num(plot_bottom)}" for x, _ in points)
            elements.append(f'<polyline class="svg-axis" points="{flat_pts}" />')
        mid_x = (plot_left + plot_right) / 2.0
        elements.append(
            f'<text class="svg-label" x="{_fmt_num(mid_x)}" y="72" text-anchor="middle">暂无记录</text>'
        )

    elements.extend(x_labels)

    aria_parts = [f"{label}{_fmt_count(count)}条" for _, label, count in rows]
    aria_label = f"近{n}日每日入站消息趋势：{'，'.join(aria_parts)}"
    svg = (
        f'<svg class="chart-svg" viewBox="0 0 360 148" role="img" '
        f'aria-label="{_escape(aria_label)}">{"".join(elements)}</svg>'
    )

    legend_items = [
        '<span class="legend-item"><span class="swatch c-green"></span>每日入站</span>',
        f'<span class="legend-item"><span class="swatch swatch--line"></span>7日均值 {avg_val:.1f}</span>',
    ]
    if peak_idx >= 0:
        peak_date, peak_label, peak_count = rows[peak_idx]
        peak_day = _extract_day_display(peak_date, peak_label)
        legend_items.append(
            '<span class="legend-item"><span class="swatch c-ink"></span>'
            f'峰值 {_escape(peak_day)} ({_fmt_count(peak_count)})</span>'
        )
    else:
        legend_items.append(
            '<span class="legend-item"><span class="swatch c-muted"></span>暂无入站记录</span>'
        )
    return f'<figure class="chart-figure">{svg}<div class="chart-legend">{"".join(legend_items)}</div></figure>'


def _format_hour_clock(hour_val: int | None, label: str) -> str:
    if hour_val is not None:
        return f"{hour_val:02d}:00"
    stripped = label.strip()
    if ":" in stripped:
        return stripped
    if stripped.isdigit():
        return f"{int(stripped):02d}:00"
    return stripped


def render_hourly_bar_chart(hourly_rows: list[dict[str, Any]]) -> str:
    """Render today's hourly inbound bar chart (`viewBox="0 0 280 148"`) and legend."""
    plot_top = 20.0
    plot_bottom = 120.0
    plot_height = plot_bottom - plot_top
    plot_right = 272.0

    rows: list[tuple[int | None, str, int]] = []
    for item in hourly_rows:
        if not isinstance(item, Mapping):
            continue
        raw_hour = item.get("hour")
        hour_int = _safe_int(raw_hour) if raw_hour is not None else None
        label = str(item.get("label", "")) or (f"{hour_int:02d}" if hour_int is not None else "")
        count = _nonneg_int(item.get("count", 0))
        rows.append((hour_int, label, count))

    if not rows:
        svg = (
            '<svg class="chart-svg" viewBox="0 0 280 148" role="img" aria-label="今日暂无分时入站记录">'
            '<line class="svg-grid" x1="22" y1="20" x2="272" y2="20" />'
            '<line class="svg-grid" x1="22" y1="70" x2="272" y2="70" />'
            '<line class="svg-axis" x1="22" y1="120" x2="272" y2="120" />'
            '<text class="svg-label" x="17" y="23" text-anchor="end">2</text>'
            '<text class="svg-label" x="17" y="73" text-anchor="end">1</text>'
            '<text class="svg-label" x="17" y="123" text-anchor="end">0</text>'
            '<text class="svg-label" x="147" y="74" text-anchor="middle">暂无记录</text>'
            "</svg>"
        )
        legend = '<div class="chart-legend"><span class="legend-item"><span class="swatch c-muted"></span>今日暂无入站记录</span></div>'
        return f'<figure class="chart-figure">{svg}{legend}</figure>'

    counts = [c for _, _, c in rows]
    max_count = max(counts)
    total = sum(counts)
    y_max = _nice_axis_max(float(max_count), intervals=2)
    tick_values = [y_max, y_max // 2, 0]
    tick_ys = [20.0, 70.0, 120.0]

    max_tick_len = max(len(_fmt_count(t)) for t in tick_values)
    plot_left = max(22.0, 7.0 + max_tick_len * 6.0)
    tick_x = plot_left - 5.0

    elements: list[str] = [
        f'<line class="svg-grid" x1="{_fmt_num(plot_left)}" y1="20" x2="{_fmt_num(plot_right)}" y2="20" />',
        f'<line class="svg-grid" x1="{_fmt_num(plot_left)}" y1="70" x2="{_fmt_num(plot_right)}" y2="70" />',
        f'<line class="svg-axis" x1="{_fmt_num(plot_left)}" y1="120" x2="{_fmt_num(plot_right)}" y2="120" />',
    ]
    for tick_val, y in zip(tick_values, tick_ys):
        elements.append(
            f'<text class="svg-label" x="{_fmt_num(tick_x)}" y="{_fmt_num(y + 3.0)}" '
            f'text-anchor="end">{_fmt_count(tick_val)}</text>'
        )

    n = len(rows)
    slot_w = (plot_right - plot_left) / n
    bar_w = min(16.0, max(4.0, slot_w * 0.62))
    skip_x_labels = n > 14
    peak_idx = counts.index(max_count) if max_count > 0 else -1

    bars: list[str] = []
    val_labels: list[str] = []
    x_labels: list[str] = []

    for idx, (_, label, count) in enumerate(rows):
        cx = plot_left + slot_w * (idx + 0.5)
        if count > 0 and max_count > 0:
            bar_h = max(2.0, (count / y_max) * plot_height)
            y = plot_bottom - bar_h
            bar_x = cx - bar_w / 2.0
            is_peak = idx == peak_idx
            if is_peak:
                bars.append(
                    f'<rect class="svg-bar--peak" x="{_fmt_num(bar_x)}" y="{_fmt_num(y)}" '
                    f'width="{_fmt_num(bar_w)}" height="{_fmt_num(bar_h)}" rx="1.5" />'
                )
            else:
                opacity = 0.48 + 0.42 * (count / max_count)
                bars.append(
                    f'<rect class="svg-bar--solid" x="{_fmt_num(bar_x)}" y="{_fmt_num(y)}" '
                    f'width="{_fmt_num(bar_w)}" height="{_fmt_num(bar_h)}" rx="1.5" opacity="{opacity:.2f}" />'
                )
            if not skip_x_labels or is_peak or idx % 2 == 0:
                val_cls = "svg-val svg-val--peak" if is_peak else "svg-val"
                val_y = max(12.0, y - 5.5)
                val_labels.append(
                    f'<text class="{val_cls}" x="{_fmt_num(cx)}" y="{_fmt_num(val_y)}" '
                    f'text-anchor="middle">{_fmt_count(count)}</text>'
                )

        if not skip_x_labels or idx % 2 == 0:
            x_labels.append(
                f'<text class="svg-label" x="{_fmt_num(cx)}" y="136" '
                f'text-anchor="middle">{_escape(label)}</text>'
            )

    elements.extend(bars)
    elements.extend(val_labels)
    if max_count == 0:
        mid_x = (plot_left + plot_right) / 2.0
        elements.append(
            f'<text class="svg-label" x="{_fmt_num(mid_x)}" y="74" text-anchor="middle">暂无记录</text>'
        )
    elements.extend(x_labels)

    aria_parts = [f"{label}时{_fmt_count(count)}条" for _, label, count in rows]
    aria_label = f"今日分时入站条数：{'，'.join(aria_parts)}"
    svg = (
        f'<svg class="chart-svg" viewBox="0 0 280 148" role="img" '
        f'aria-label="{_escape(aria_label)}">{"".join(elements)}</svg>'
    )

    legend_items: list[str] = []
    if peak_idx >= 0:
        peak_h, peak_l, peak_c = rows[peak_idx]
        legend_items.append(
            '<span class="legend-item"><span class="swatch c-ink"></span>'
            f'高峰 {_escape(_format_hour_clock(peak_h, peak_l))} ({_fmt_count(peak_c)})</span>'
        )
        other_nonzero = [
            (idx, h_val, lbl, cnt)
            for idx, (h_val, lbl, cnt) in enumerate(rows)
            if idx != peak_idx and cnt > 0
        ]
        if other_nonzero:
            _, sec_h, sec_l, sec_c = max(other_nonzero, key=lambda item: item[3])
            legend_items.append(
                '<span class="legend-item"><span class="swatch c-green"></span>'
                f'次峰 {_escape(_format_hour_clock(sec_h, sec_l))} ({_fmt_count(sec_c)})</span>'
            )
        else:
            legend_items.append(
                f'<span class="legend-item"><span class="swatch c-green"></span>今日共 {_fmt_count(total)} 条</span>'
            )
    else:
        legend_items.append(
            '<span class="legend-item"><span class="swatch c-muted"></span>今日暂无入站记录</span>'
        )
    return f'<figure class="chart-figure">{svg}<div class="chart-legend">{"".join(legend_items)}</div></figure>'


def render_grouped_pace_chart(
    hourly_pace: list[dict[str, Any]],
    *,
    total_created: int,
    total_closed: int,
) -> str:
    """Render the librarian '今日入队 vs 已结' grouped bar chart (`viewBox="0 0 420 104"`)."""
    plot_top = 14.0
    plot_bottom = 80.0
    plot_height = plot_bottom - plot_top
    plot_right = 408.0
    tot_created = _nonneg_int(total_created)
    tot_closed = _nonneg_int(total_closed)

    rows: list[tuple[int | None, str, int, int]] = []
    for item in hourly_pace:
        if not isinstance(item, Mapping):
            continue
        raw_hour = item.get("hour")
        hour_int = _safe_int(raw_hour) if raw_hour is not None else None
        label = str(item.get("label", "")) or (f"{hour_int:02d}:00" if hour_int is not None else "")
        created = _nonneg_int(item.get("created", 0))
        closed = _nonneg_int(item.get("closed", 0))
        rows.append((hour_int, label, created, closed))

    if not rows:
        svg = (
            '<svg class="chart-svg" viewBox="0 0 420 104" role="img" aria-label="今日暂无入队与已结记录">'
            '<line class="svg-grid" x1="24" y1="14" x2="408" y2="14" />'
            '<line class="svg-grid" x1="24" y1="47" x2="408" y2="47" />'
            '<line class="svg-axis" x1="24" y1="80" x2="408" y2="80" />'
            '<text class="svg-label" x="18" y="17" text-anchor="end">2</text>'
            '<text class="svg-label" x="18" y="50" text-anchor="end">1</text>'
            '<text class="svg-label" x="18" y="83" text-anchor="end">0</text>'
            '<text class="svg-label" x="216" y="51" text-anchor="middle">暂无记录</text>'
            "</svg>"
        )
        legend = (
            '<div class="chart-legend">'
            f'<span class="legend-item"><span class="swatch c-muted"></span>入队 {_fmt_count(tot_created)}</span>'
            f'<span class="legend-item"><span class="swatch c-green"></span>已结 {_fmt_count(tot_closed)}</span>'
            "</div>"
        )
        return f'<figure class="chart-figure">{svg}{legend}</figure>'

    max_val = max(max(c, d) for _, _, c, d in rows)
    y_max = _nice_axis_max(float(max_val), intervals=2)
    tick_values = [y_max, y_max // 2, 0]
    tick_ys = [14.0, 47.0, 80.0]

    max_tick_len = max(len(_fmt_count(t)) for t in tick_values)
    plot_left = max(24.0, 8.0 + max_tick_len * 6.0)
    tick_x = plot_left - 6.0

    elements: list[str] = [
        f'<line class="svg-grid" x1="{_fmt_num(plot_left)}" y1="14" x2="{_fmt_num(plot_right)}" y2="14" />',
        f'<line class="svg-grid" x1="{_fmt_num(plot_left)}" y1="47" x2="{_fmt_num(plot_right)}" y2="47" />',
        f'<line class="svg-axis" x1="{_fmt_num(plot_left)}" y1="80" x2="{_fmt_num(plot_right)}" y2="80" />',
    ]
    for tick_val, y in zip(tick_values, tick_ys):
        elements.append(
            f'<text class="svg-label" x="{_fmt_num(tick_x)}" y="{_fmt_num(y + 3.0)}" '
            f'text-anchor="end">{_fmt_count(tick_val)}</text>'
        )

    n = len(rows)
    slot_w = (plot_right - plot_left) / n
    group_gap = 2.0 if slot_w >= 20.0 else 1.0
    bar_w = min(13.0, max(2.5, (slot_w * 0.62 - group_gap) / 2.0))
    skip_x_labels = n > 12

    bars: list[str] = []
    x_labels: list[str] = []
    for idx, (_, label, created, closed) in enumerate(rows):
        cx = plot_left + slot_w * (idx + 0.5)
        left_x = cx - group_gap / 2.0 - bar_w
        right_x = cx + group_gap / 2.0

        if created > 0 and y_max > 0:
            c_h = max(2.0, (created / y_max) * plot_height)
            bars.append(
                f'<rect class="svg-bar--secondary" x="{_fmt_num(left_x)}" y="{_fmt_num(plot_bottom - c_h)}" '
                f'width="{_fmt_num(bar_w)}" height="{_fmt_num(c_h)}" rx="1.5" />'
            )
        if closed > 0 and y_max > 0:
            d_h = max(2.0, (closed / y_max) * plot_height)
            bars.append(
                f'<rect class="svg-bar--solid" x="{_fmt_num(right_x)}" y="{_fmt_num(plot_bottom - d_h)}" '
                f'width="{_fmt_num(bar_w)}" height="{_fmt_num(d_h)}" rx="1.5" />'
            )

        if not skip_x_labels or idx % 2 == 0:
            x_labels.append(
                f'<text class="svg-label" x="{_fmt_num(cx)}" y="96" '
                f'text-anchor="middle">{_escape(label)}</text>'
            )

    elements.extend(bars)
    if max_val == 0:
        mid_x = (plot_left + plot_right) / 2.0
        elements.append(
            f'<text class="svg-label" x="{_fmt_num(mid_x)}" y="51" text-anchor="middle">暂无记录</text>'
        )
    elements.extend(x_labels)

    first_lbl = rows[0][1]
    last_lbl = rows[-1][1]
    aria_label = (
        f"{first_lbl}至{last_lbl}每小时入队与已结数量对比，"
        f"累计入队{_fmt_count(tot_created)}项，已结{_fmt_count(tot_closed)}项"
    )
    svg = (
        f'<svg class="chart-svg" viewBox="0 0 420 104" role="img" '
        f'aria-label="{_escape(aria_label)}">{"".join(elements)}</svg>'
    )

    legend_items = [
        f'<span class="legend-item"><span class="swatch c-muted"></span>入队 {_fmt_count(tot_created)}</span>',
        f'<span class="legend-item"><span class="swatch c-green"></span>已结 {_fmt_count(tot_closed)}</span>',
    ]
    max_created = max(c for _, _, c, _ in rows)
    if max_created > 0:
        peak_rows = [(h_val, lbl) for h_val, lbl, c, _ in rows if c == max_created]
        if len(peak_rows) >= 2 and all(h is not None for h, _ in peak_rows):
            hours = [h for h, _ in peak_rows if h is not None]
            peak_text = f"{min(hours):02d}–{max(hours):02d}h 为入队高峰"
        else:
            h_val, lbl = peak_rows[0]
            peak_text = f"{_format_hour_clock(h_val, lbl)} 为入队高峰"
        legend_items.append(
            f'<span class="legend-item"><span class="swatch c-ochre"></span>{_escape(peak_text)}</span>'
        )
    return f'<figure class="chart-figure">{svg}<div class="chart-legend">{"".join(legend_items)}</div></figure>'


def _dict(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = mapping.get(key)
    return val if isinstance(val, Mapping) else {}


def _list(mapping: Mapping[str, Any], key: str) -> list[Any]:
    val = mapping.get(key)
    return list(val) if isinstance(val, list) else []


def _extract_hm(value: str) -> str:
    text = value.strip()
    if len(text) >= 16 and text[10] in {" ", "T"} and ":" in text[11:16]:
        return text[11:16]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text


def _render_alert_rows(alerts: Sequence[Any]) -> str:
    rows: list[str] = []
    for raw in alerts:
        if not isinstance(raw, Mapping):
            continue
        raw_time = str(
            raw.get("time_hm")
            or raw.get("created_hm")
            or raw.get("hm")
            or raw.get("time")
            or raw.get("created_at_utc8")
            or raw.get("created_at")
            or ""
        )
        time_hm = _extract_hm(raw_time) or "--:--"
        iso_dt = str(raw.get("datetime") or raw.get("created_at_utc8") or raw.get("created_at") or "").strip()

        kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()
        default_label, default_cls = _ALERT_KIND_DEFAULTS.get(kind, ("异常", "muted"))
        badge_label = str(raw.get("badge") or raw.get("label") or default_label)

        raw_cls = str(raw.get("badge_class") or raw.get("level") or raw.get("severity") or default_cls).strip().lower()
        if raw_cls in {"error", "high", "critical", "rust"}:
            badge_cls = "urgent"
        elif raw_cls in {"warn", "warning", "medium"}:
            badge_cls = "ochre"
        elif raw_cls in _SAFE_BADGE_CLASSES:
            badge_cls = raw_cls
        else:
            badge_cls = default_cls

        text = str(raw.get("text") or raw.get("summary") or raw.get("message") or raw.get("detail") or "")
        dt_attr = f' datetime="{_escape(iso_dt)}"' if iso_dt else ""
        rows.append(
            '<li class="alert-row">'
            f"<time{dt_attr}>{_escape(time_hm)}</time>"
            f'<span class="badge {badge_cls}">{_escape(badge_label)}</span>'
            f'<span class="alert-text">{_escape(text)}</span>'
            "</li>"
        )
    if not rows:
        return '<p class="panel-note">暂无记录</p>'
    return f'<ol class="alert-list">{"".join(rows)}</ol>'


def render_triage_audit_panel(recent_triage_runs: Sequence[Mapping[str, Any]]) -> str:
    """Render sanitized recent triage routing audit panel (metadata only, no PII)."""
    rows_clean = [row for row in recent_triage_runs if isinstance(row, Mapping)]
    header = (
        '<div class="panel-head">'
        '<h2 id="triage-audit-title">最近分诊路由审计</h2>'
        f'<span class="period-chip">最近 {_fmt_count(len(rows_clean))} 次 · 仅路由元数据</span>'
        "</div>"
    )
    if not rows_clean:
        return (
            '<section class="panel panel--span-12" aria-labelledby="triage-audit-title">'
            f'{header}<p class="panel-note">暂无分诊记录。</p>'
            "</section>"
        )

    items: list[str] = []
    for row in rows_clean:
        run_id = _safe_int(row.get("run_id", 0))
        time_utc8 = str(row.get("time_utc8") or "--:--")
        stage_label = str(row.get("stage_label") or row.get("stage_path") or "未知路径")
        stage_path = str(row.get("stage_path") or "")
        severity = str(row.get("severity") or "muted").strip().lower()
        if severity not in ("good", "ochre", "urgent", "muted"):
            severity = "muted"

        meta_parts: list[str] = []
        preset_id = str(row.get("preset_id") or "").strip()
        section_title = str(row.get("section_title") or "").strip()
        decision_route = str(row.get("decision_route") or "").strip()
        if preset_id:
            meta_parts.append(f"预设 {preset_id}")
        if section_title:
            meta_parts.append(f"出处「{section_title}」")
        if decision_route:
            conf = row.get("decision_confidence")
            if conf is not None:
                try:
                    meta_parts.append(f"决策 {decision_route} ({float(conf):.2f})")
                except (TypeError, ValueError):
                    meta_parts.append(f"决策 {decision_route}")
            else:
                meta_parts.append(f"决策 {decision_route}")
        if row.get("clarified_already"):
            meta_parts.append("澄清后续判")
        if not meta_parts and stage_path:
            meta_parts.append(stage_path)

        dur_ms = row.get("decision_duration_ms")
        dur_html = ""
        if dur_ms is not None:
            try:
                dur_html = f'<span class="triage-ms">{int(dur_ms)}ms</span>'
            except (TypeError, ValueError):
                dur_html = ""

        case_id = row.get("staff_case_id")
        case_html = (
            f'<span class="badge ochre">工单 #{int(case_id)}</span>'
            if case_id is not None and _safe_int(case_id) > 0
            else ""
        )

        items.append(
            '<li class="triage-row">'
            f'<time class="inbox-time">{_escape(time_utc8)}</time>'
            f'<span class="badge {_escape(severity)}">{_escape(stage_label)}</span>'
            f'<span class="triage-meta">{_escape(" · ".join(meta_parts))}</span>'
            f'<span class="triage-tail">{dur_html}{case_html}<span class="triage-id">#{run_id}</span></span>'
            "</li>"
        )

    return (
        '<section class="panel panel--span-12" aria-labelledby="triage-audit-title">'
        f'{header}<ol class="triage-list">{"".join(items)}</ol>'
        "</section>"
    )


def render_admin_overview(analytics: dict[str, Any], *, reply_mode_text: str) -> str:
    """Render the Admin KPI strip, reply mode banner, and 6-panel chart grid.

    Returns a pure HTML fragment (no ``<html>``/``<body>`` wrapper) so
    ``dashboard.py`` can compose it with the page frame and allowlist panel.
    """
    data: Mapping[str, Any] = analytics if isinstance(analytics, Mapping) else {}

    daily_inbound = _list(data, "daily_inbound")
    hourly_inbound = _list(data, "hourly_inbound_today")
    inbound_7d_total = _nonneg_int(data.get("inbound_7d_total", 0))
    inbound_7d_avg = _nonneg_float(data.get("inbound_7d_avg", 0.0))
    inbound_today_total = _nonneg_int(data.get("inbound_today_total", 0))
    inbound_all_total = _nonneg_int(data.get("inbound_all_total", 0))

    replies_today = _dict(data, "replies_today")
    rt_sent = _nonneg_int(replies_today.get("sent", 0))
    rt_shadow = _nonneg_int(replies_today.get("shadow", 0))
    rt_failed = _nonneg_int(replies_today.get("failed", 0))
    rt_total = _nonneg_int(replies_today.get("total", rt_sent + rt_shadow + rt_failed))

    replies_all = _dict(data, "replies_all")
    ra_sent = _nonneg_int(replies_all.get("sent", 0))
    ra_shadow = _nonneg_int(replies_all.get("shadow", 0))
    ra_failed = _nonneg_int(replies_all.get("failed", 0))
    ra_total = _nonneg_int(replies_all.get("total", ra_sent + ra_shadow + ra_failed))

    filter_7d = _dict(data, "filter_7d")
    f_question = _nonneg_int(filter_7d.get("question", 0))
    f_feedback = _nonneg_int(filter_7d.get("feedback", 0))
    f_invalid = _nonneg_int(filter_7d.get("invalid", 0))
    f_unsupported = _nonneg_int(filter_7d.get("unsupported", 0))
    f_emergency = _nonneg_int(filter_7d.get("emergency", 0))
    f_gray = _nonneg_int(filter_7d.get("gray_enroll", 0))
    f_sum = f_question + f_feedback + f_invalid + f_unsupported + f_emergency
    f_total = _nonneg_int(filter_7d.get("total_non_gray", f_sum))
    if f_total == 0 and f_sum > 0:
        f_total = f_sum

    routing_7d = _dict(data, "routing_7d")
    r_tier2 = _nonneg_int(routing_7d.get("tier2_rag", 0))
    r_tier1 = _nonneg_int(routing_7d.get("tier1_preset", 0))
    r_clarify = _nonneg_int(routing_7d.get("clarify", 0))
    r_fallback = _nonneg_int(routing_7d.get("staff_fallback", 0))
    r_other = _nonneg_int(routing_7d.get("other", 0))
    r_sum = r_tier2 + r_tier1 + r_clarify + r_fallback + r_other
    r_total = _nonneg_int(routing_7d.get("total", r_sum))
    if r_total == 0 and r_sum > 0:
        r_total = r_sum
    if "auto_rate_pct" in routing_7d:
        auto_rate_pct = min(100.0, _nonneg_float(routing_7d.get("auto_rate_pct", 0.0)))
    elif r_total > 0:
        auto_rate_pct = min(100.0, ((r_tier2 + r_tier1 + r_clarify) / r_total) * 100.0)
    else:
        auto_rate_pct = 0.0

    batches = _dict(data, "batches")
    b_open = _nonneg_int(batches.get("open", 0))
    b_processing = _nonneg_int(batches.get("processing", 0))
    b_queued = _nonneg_int(batches.get("queued", 0))
    b_active = _nonneg_int(batches.get("active_total", b_open + b_processing + b_queued))

    open_cases = _dict(data, "open_cases")
    c_emergency = _nonneg_int(open_cases.get("emergency", 0))
    c_todo = _nonneg_int(open_cases.get("staff_todo", 0))
    c_total = _nonneg_int(open_cases.get("total", c_emergency + c_todo))

    recent_alerts = _list(data, "recent_alerts")
    recent_triage_runs = _list(data, "recent_triage_runs")
    menu_events_7d = _nonneg_int(data.get("menu_events_7d", 0))
    snapshot_hm = _extract_hm(str(data.get("snapshot_at_utc8", ""))) or "实时"

    # KPI 1: 7-day inbound + sparkline
    spark_values = [
        _nonneg_int(row.get("count", 0)) for row in daily_inbound if isinstance(row, Mapping)
    ]
    kpi1_delta = f"今日 {_fmt_count(inbound_today_total)}"
    kpi1_html = (
        '<article class="metric">'
        '<div class="metric-top">'
        '<h2 class="metric-label">7日入站</h2>'
        f'<span class="metric-delta">{kpi1_delta}</span>'
        "</div>"
        '<div class="metric-mid">'
        f'<p class="metric-value">{_fmt_count(inbound_7d_total)}<span class="metric-unit">条</span></p>'
        f"{render_sparkline(spark_values)}"
        "</div>"
        f'<p class="metric-note">日均 {inbound_7d_avg:.1f} · 今日 {_fmt_count(inbound_today_total)} · 累计 {_fmt_count(inbound_all_total)}</p>'
        "</article>"
    )

    # KPI 2: Today replies + micro-bar
    if rt_total > 0:
        kpi2_delta = f"{_fmt_pct(float(rt_sent), float(rt_total))} 实发"
    else:
        kpi2_delta = f"累计 {_fmt_count(ra_total)}"
    kpi2_html = (
        '<article class="metric">'
        '<div class="metric-top">'
        '<h2 class="metric-label">今日回复</h2>'
        f'<span class="metric-delta">{kpi2_delta}</span>'
        "</div>"
        '<div class="metric-mid">'
        f'<p class="metric-value">{_fmt_count(rt_total)}<span class="metric-unit">条</span></p>'
        "</div>"
        f'{render_micro_bar([("c-green", rt_sent), ("c-ochre", rt_shadow), ("c-rust", rt_failed)])}'
        f'<p class="metric-note">实发{_fmt_count(rt_sent)} · 影子{_fmt_count(rt_shadow)} · 失败{_fmt_count(rt_failed)}</p>'
        "</article>"
    )

    # KPI 3: Active batches + micro-bar + auto rate
    kpi3_delta = f"自动 {auto_rate_pct:.1f}%" if r_total > 0 else "暂无问答"
    kpi3_html = (
        '<article class="metric">'
        '<div class="metric-top">'
        '<h2 class="metric-label">未结批次</h2>'
        f'<span class="metric-delta">{kpi3_delta}</span>'
        "</div>"
        '<div class="metric-mid">'
        f'<p class="metric-value">{_fmt_count(b_active)}<span class="metric-unit">批</span></p>'
        "</div>"
        f'{render_micro_bar([("c-teal", b_open), ("c-green", b_processing), ("c-ochre", b_queued)])}'
        f'<p class="metric-note">待处理{_fmt_count(b_open)} · 处理中{_fmt_count(b_processing)} · 已排队{_fmt_count(b_queued)}</p>'
        "</article>"
    )

    # KPI 4: Open cases + micro-bar + emergency warn badge
    if c_emergency > 0:
        kpi4_delta_html = f'<span class="metric-delta metric-delta--warn">含 {_fmt_count(c_emergency)} 紧急</span>'
    else:
        kpi4_delta_html = '<span class="metric-delta">无紧急</span>'
    kpi4_html = (
        '<article class="metric">'
        '<div class="metric-top">'
        '<h2 class="metric-label">未结待办</h2>'
        f"{kpi4_delta_html}"
        "</div>"
        '<div class="metric-mid">'
        f'<p class="metric-value">{_fmt_count(c_total)}<span class="metric-unit">项</span></p>'
        "</div>"
        f'{render_micro_bar([("c-rust", c_emergency), ("c-ink", c_todo)])}'
        f'<p class="metric-note">普通待办 {_fmt_count(c_todo)} · 紧急事件 {_fmt_count(c_emergency)}</p>'
        "</article>"
    )

    metric_strip = f'<section class="metric-strip" aria-label="关键指标">{kpi1_html}{kpi2_html}{kpi3_html}{kpi4_html}</section>'
    status_banner = (
        f'<p class="status-note" role="status">{_escape(reply_mode_text)}</p>'
        if reply_mode_text
        else ""
    )

    # Row 1 Panel 1: 7-day trend
    panel_trend = (
        '<section class="panel panel--span-5 panel--trend-wide" aria-labelledby="traffic-title">'
        '<div class="panel-head">'
        '<h2 id="traffic-title">近 7 日入站趋势</h2>'
        f'<span class="period-chip">合计 {_fmt_count(inbound_7d_total)} · 均 {inbound_7d_avg:.1f}/日</span>'
        "</div>"
        f"{render_daily_trend_chart(daily_inbound, avg=inbound_7d_avg)}"
        "</section>"
    )

    # Row 1 Panel 2: Today hourly inbound
    hourly_clean = [row for row in hourly_inbound if isinstance(row, Mapping)]
    hourly_sum = sum(_nonneg_int(row.get("count", 0)) for row in hourly_clean)
    if hourly_clean:
        first_lbl = str(hourly_clean[0].get("label", "")) or f"{_safe_int(hourly_clean[0].get('hour', 0)):02d}"
        last_lbl = str(hourly_clean[-1].get("label", "")) or f"{_safe_int(hourly_clean[-1].get('hour', 0)):02d}"
        hourly_chip = f"{_escape(first_lbl)}–{_escape(last_lbl)}h · {_fmt_count(hourly_sum)} 条"
    else:
        hourly_chip = f"今日 · {_fmt_count(hourly_sum)} 条"
    panel_hourly = (
        '<section class="panel panel--span-4" aria-labelledby="hourly-title">'
        '<div class="panel-head">'
        '<h2 id="hourly-title">今日分时入站</h2>'
        f'<span class="period-chip">{hourly_chip}</span>'
        "</div>"
        f"{render_hourly_bar_chart(hourly_inbound)}"
        "</section>"
    )

    # Row 1 Panel 3: Reply outcomes donut (today if > 0, else all-time fallback with "累计" chip)
    if rt_total > 0:
        outcome_title = "今日回复结果"
        outcome_chip = f"{_fmt_count(rt_total)} 条"
        d_sent, d_shadow, d_failed, d_total = rt_sent, rt_shadow, rt_failed, rt_total
        d_sub = "今日回复"
        d_scope = "今日"
    else:
        outcome_title = "回复结果"
        outcome_chip = "累计"
        d_sent, d_shadow, d_failed, d_total = ra_sent, ra_shadow, ra_failed, ra_total
        d_sub = "累计回复" if d_total > 0 else "暂无记录"
        d_scope = "累计"

    donut_aria = (
        f"{d_scope}{_fmt_count(d_total)}条回复结果："
        f"实际发送{_fmt_count(d_sent)}条占{_fmt_pct(float(d_sent), float(d_total))}，"
        f"影子模式{_fmt_count(d_shadow)}条占{_fmt_pct(float(d_shadow), float(d_total))}，"
        f"发送失败{_fmt_count(d_failed)}条占{_fmt_pct(float(d_failed), float(d_total))}"
    )
    reply_donut = render_donut(
        [
            ("实际发送", "c-green", "var(--green)", d_sent),
            ("影子模式", "c-ochre", "var(--ochre)", d_shadow),
            ("发送失败", "c-rust", "var(--rust)", d_failed),
        ],
        center_num=_fmt_count(d_total),
        center_sub=d_sub,
        aria_label=donut_aria,
        small=False,
        stack=True,
    )
    panel_outcomes = (
        '<section class="panel panel--span-3" aria-labelledby="outcomes-title">'
        '<div class="panel-head">'
        f'<h2 id="outcomes-title">{outcome_title}</h2>'
        f'<span class="period-chip">{outcome_chip}</span>'
        "</div>"
        f"{reply_donut}"
        '<p class="panel-footnote">实际发送指微信接口已受理（不代表已读） · 影子模式仅记录未发信</p>'
        "</section>"
    )

    # Row 2 Panel 4: Filter categories
    filter_items = [
        ("问答", "c-green", f_question),
        ("意见反馈", "c-teal", f_feedback),
        ("无效输入", "c-slate", f_invalid),
        ("不支持", "c-muted", f_unsupported),
        ("紧急情况", "c-rust", f_emergency),
    ]
    filter_aria = (
        f"近7日Filter分类：问答{_fmt_count(f_question)}条，意见反馈{_fmt_count(f_feedback)}条，"
        f"无效输入{_fmt_count(f_invalid)}条，不支持{_fmt_count(f_unsupported)}条，紧急情况{_fmt_count(f_emergency)}条"
    )
    filter_notes: list[str] = [
        f"不含灰度口令入组（{_fmt_count(f_gray)} 条）" if f_gray > 0 else "不含灰度口令入组"
    ]
    if menu_events_7d > 0:
        filter_notes.append(f"已剔除菜单/事件推送 {_fmt_count(menu_events_7d)} 条")
    gray_note = " · ".join(filter_notes)
    panel_categories = (
        '<section class="panel panel--span-4" aria-labelledby="categories-title">'
        '<div class="panel-head">'
        '<h2 id="categories-title">Filter 分类分布</h2>'
        f'<span class="period-chip">7 日 · {_fmt_count(f_total)} 条</span>'
        "</div>"
        f"{render_stack_hbar_list(filter_items, denominator=f_total, wide_label=False, show_stack_bar=True, aria_label=filter_aria)}"
        f'<p class="panel-footnote">{gray_note}</p>'
        "</section>"
    )

    # Row 2 Panel 5: Routing breakdown
    routing_items = [
        ("Tier 2 检索", "c-teal", r_tier2),
        ("Tier 1 预设", "c-green", r_tier1),
        ("单次澄清", "c-ochre", r_clarify),
        ("馆员兜底", "c-rust", r_fallback),
    ]
    if r_other > 0:
        routing_items.append(("其他处理", "c-slate", r_other))
    routing_badge = (
        f'<span class="badge good">自动 {auto_rate_pct:.1f}%</span>'
        if r_total > 0
        else '<span class="badge muted">暂无问答</span>'
    )
    routing_aria = (
        f"近7日{_fmt_count(r_total)}条问答分流：Tier 2检索{_fmt_count(r_tier2)}条，"
        f"Tier 1预设{_fmt_count(r_tier1)}条，单次澄清{_fmt_count(r_clarify)}条，馆员兜底{_fmt_count(r_fallback)}条"
    )
    panel_routing = (
        '<section class="panel panel--span-4" aria-labelledby="routing-title">'
        '<div class="panel-head">'
        '<h2 id="routing-title">问答分流路径</h2>'
        f"{routing_badge}"
        "</div>"
        f"{render_stack_hbar_list(routing_items, denominator=r_total, wide_label=True, show_stack_bar=True, aria_label=routing_aria)}"
        f'<p class="panel-footnote">分母为近 7 日 {_fmt_count(r_total)} 条 question</p>'
        "</section>"
    )

    # Row 2 Panel 6: Queue & Alerts
    urgent_strong_cls = ' class="urgent-num"' if c_emergency > 0 else ""
    panel_operations = (
        '<section class="panel panel--span-4" aria-labelledby="operations-title">'
        '<div class="panel-head">'
        '<h2 id="operations-title">队列与异常</h2>'
        f'<span class="period-chip">{_escape(snapshot_hm)} 快照</span>'
        "</div>"
        '<div class="mini-section">'
        '<div class="mini-head"><span>未结问答批次</span>'
        f'<span>共 {_fmt_count(b_active)} 批</span></div>'
        f'{_render_stack_bar_html([("c-teal", b_open), ("c-green", b_processing), ("c-ochre", b_queued)])}'
        '<div class="inline-kv">'
        f'<span>待处理<strong>{_fmt_count(b_open)}</strong></span>'
        f'<span>处理中<strong>{_fmt_count(b_processing)}</strong></span>'
        f'<span>已排队<strong>{_fmt_count(b_queued)}</strong></span>'
        "</div></div>"
        '<div class="mini-section">'
        '<div class="mini-head"><span>未结馆员待办</span>'
        f'<span>共 {_fmt_count(c_total)} 项</span></div>'
        f'{_render_stack_bar_html([("c-rust", c_emergency), ("c-ink", c_todo)])}'
        '<div class="inline-kv">'
        f'<span>普通待办<strong>{_fmt_count(c_todo)}</strong></span>'
        f'<span>紧急事件<strong{urgent_strong_cls}>{_fmt_count(c_emergency)}</strong></span>'
        "</div></div>"
        '<div class="mini-section">'
        '<div class="mini-head"><span>最近异常</span>'
        f'<span>{_fmt_count(len(recent_alerts))} 条</span></div>'
        f"{_render_alert_rows(recent_alerts)}"
        "</div></section>"
    )

    panel_triage = render_triage_audit_panel(recent_triage_runs)
    grid = (
        '<div class="dashboard-grid" aria-label="运营图表">'
        f"{panel_trend}{panel_hourly}{panel_outcomes}"
        f"{panel_categories}{panel_routing}{panel_operations}"
        f"{panel_triage}"
        "</div>"
    )
    return f"{metric_strip}{status_banner}{grid}"
