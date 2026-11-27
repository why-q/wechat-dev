"""Unit tests for inline SVG chart and admin overview renderers."""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET

from support_backend.dashboard_charts import (
    render_admin_overview,
    render_daily_trend_chart,
    render_donut,
    render_grouped_pace_chart,
    render_hourly_bar_chart,
    render_micro_bar,
    render_sparkline,
    render_stack_hbar_list,
)


def _assert_no_bad_numbers(test: unittest.TestCase, markup: str) -> None:
    test.assertNotIn("NaN", markup)
    test.assertNotIn("nan", markup.lower())
    test.assertNotIn("Infinity", markup)
    test.assertNotIn("inf", re.findall(r"\b[+-]?inf\b", markup.lower()))


class DashboardChartsTests(unittest.TestCase):
    def test_render_sparkline_empty_zero_constant_and_populated(self):
        for bad_input in ([], [0, 0, 0], [float("nan"), float("inf")]):
            out = render_sparkline(bad_input)
            self.assertIn('<svg class="metric-spark" viewBox="0 0 64 24" aria-hidden="true">', out)
            self.assertIn('class="svg-axis"', out)
            _assert_no_bad_numbers(self, out)

        single = render_sparkline([12])
        self.assertIn('<polygon class="svg-area"', single)
        self.assertIn('<polyline class="svg-line"', single)
        self.assertIn("<circle ", single)
        _assert_no_bad_numbers(self, single)

        flat_nonzero = render_sparkline([10, 10, 10, 10])
        self.assertIn('<polyline class="svg-line"', flat_nonzero)
        _assert_no_bad_numbers(self, flat_nonzero)

        populated = render_sparkline([34, 43, 46, 39, 52, 55, 47])
        self.assertIn('<polygon class="svg-area"', populated)
        self.assertIn('<polyline class="svg-line"', populated)
        self.assertIn('fill="var(--ink)"', populated)
        _assert_no_bad_numbers(self, populated)

    def test_render_micro_bar_empty_populated_and_tiny_slice(self):
        empty = render_micro_bar([])
        self.assertEqual(empty, '<div class="micro-bar" aria-hidden="true"></div>')

        zeros = render_micro_bar([("c-green", 0), ("c-rust", 0)])
        self.assertEqual(zeros, '<div class="micro-bar" aria-hidden="true"></div>')

        normal = render_micro_bar([("c-green", 34), ("c-ochre", 7), ("c-rust", 2)])
        self.assertIn("79.1%", normal)
        self.assertIn("16.3%", normal)
        self.assertIn("4.7%", normal)
        _assert_no_bad_numbers(self, normal)

        tiny = render_micro_bar([("c-green", 10000), ("c-rust", 1)])
        self.assertIn('class="c-rust" style="--w: 2.0%"', tiny)
        self.assertIn('class="c-green" style="--w: 98.0%"', tiny)
        _assert_no_bad_numbers(self, tiny)

    def test_render_donut_standard_small_empty_and_single(self):
        populated = render_donut(
            [
                ("实际发送", "c-green", "var(--green)", 34),
                ("影子模式", "c-ochre", "var(--ochre)", 7),
                ("发送失败", "c-rust", "var(--rust)", 2),
            ],
            center_num="43",
            center_sub="今日回复",
            aria_label="回复分布",
            small=False,
            stack=True,
        )
        self.assertIn('class="donut-layout donut-layout--stack"', populated)
        self.assertIn('viewBox="0 0 116 116"', populated)
        self.assertIn('r="42"', populated)
        self.assertIn('stroke-dasharray="206.7 263.89"', populated)
        self.assertIn("79.1%", populated)
        self.assertIn("16.3%", populated)
        self.assertIn("4.7%", populated)
        _assert_no_bad_numbers(self, populated)

        small_donut = render_donut(
            [
                ("紧急事项", "c-rust", "var(--rust)", 1),
                ("等待 ≥60m", "c-ochre", "var(--ochre)", 3),
                ("30–60m", "c-teal", "var(--teal)", 1),
                ("<30m", "c-green", "var(--green)", 2),
            ],
            center_num="7",
            center_sub="待跟进",
            aria_label="待跟进分布",
            small=True,
            stack=False,
        )
        self.assertIn('class="donut-svg donut-svg--sm"', small_donut)
        self.assertIn('viewBox="0 0 100 100"', small_donut)
        self.assertIn('r="36"', small_donut)
        self.assertIn("&lt;30m", small_donut)
        self.assertNotIn("<30m", small_donut)
        _assert_no_bad_numbers(self, small_donut)

        zero_donut = render_donut(
            [("实际发送", "c-green", "var(--green)", 0)],
            center_num="0",
            center_sub="暂无记录",
            aria_label="空分布",
        )
        self.assertIn('class="donut-track"', zero_donut)
        self.assertNotIn("stroke-dasharray", zero_donut)
        self.assertIn("0.0%", zero_donut)
        _assert_no_bad_numbers(self, zero_donut)

        single_full = render_donut(
            [("实际发送", "c-green", "var(--green)", 1500), ("失败", "c-rust", "var(--rust)", 0)],
            center_num="1,500",
            center_sub="累计回复",
            aria_label="全部成功",
        )
        self.assertIn("1,500", single_full)
        self.assertIn("100.0%", single_full)
        self.assertIn('stroke-dasharray="263.9 263.89"', single_full)
        _assert_no_bad_numbers(self, single_full)

    def test_render_stack_hbar_list_edges_and_formatting(self):
        empty = render_stack_hbar_list([])
        self.assertIn("暂无记录", empty)
        self.assertIn('<div class="stack-bar" aria-hidden="true"></div>', empty)

        all_zero = render_stack_hbar_list(
            [("问答", "c-green", 0), ("紧急", "c-rust", 0)],
            denominator=0,
            wide_label=True,
        )
        self.assertIn("hbar-row hbar-row--wide-label", all_zero)
        self.assertIn("--bar-size: 0.0%", all_zero)
        self.assertIn("0.0%", all_zero)
        _assert_no_bad_numbers(self, all_zero)

        populated = render_stack_hbar_list(
            [
                ("问答", "c-green", 1200),
                ("紧急情况", "c-rust", 3),
            ],
            denominator=1203,
            aria_label="分类统计",
        )
        self.assertIn("1,200", populated)
        self.assertIn("99.8%", populated)
        self.assertIn("0.2%", populated)
        # Tiny non-zero slice keeps at least 2.0% visual width
        self.assertIn("--bar-size: 2.0%", populated)
        self.assertIn('aria-label="分类统计"', populated)
        _assert_no_bad_numbers(self, populated)

    def test_render_daily_trend_chart_empty_zero_normal_and_large(self):
        empty = render_daily_trend_chart([], avg=0.0)
        self.assertIn('viewBox="0 0 360 148"', empty)
        self.assertIn("暂无记录", empty)
        _assert_no_bad_numbers(self, empty)

        all_zero = render_daily_trend_chart(
            [{"date": f"2026-11-{d:02d}", "label": f"{d}日", "count": 0} for d in range(21, 28)],
            avg=0.0,
        )
        self.assertIn("暂无记录", all_zero)
        self.assertIn("21日", all_zero)
        self.assertNotIn("svg-bar--peak", all_zero)
        _assert_no_bad_numbers(self, all_zero)

        demo_rows = [
            {"date": "2026-11-21", "label": "21六", "count": 34},
            {"date": "2026-11-22", "label": "22日", "count": 43},
            {"date": "2026-11-23", "label": "23一", "count": 46},
            {"date": "2026-11-24", "label": "24二", "count": 39},
            {"date": "2026-11-25", "label": "25三", "count": 52},
            {"date": "2026-11-26", "label": "26四", "count": 55},
            {"date": "2026-11-27", "label": "27五", "count": 47},
        ]
        normal = render_daily_trend_chart(demo_rows, avg=45.1)
        self.assertIn('viewBox="0 0 360 148"', normal)
        self.assertIn('class="svg-avg"', normal)
        self.assertIn('class="svg-bar--peak"', normal)
        self.assertIn('class="svg-dot--peak"', normal)
        self.assertIn('class="svg-val svg-val--peak"', normal)
        self.assertIn(">60</text>", normal)
        self.assertIn(">40</text>", normal)
        self.assertIn(">20</text>", normal)
        self.assertIn("峰值 26日 (55)", normal)
        self.assertIn("7日均值 45.1", normal)
        _assert_no_bad_numbers(self, normal)

        large_rows = [
            {"date": "2026-11-26", "label": "26四", "count": 12500},
            {"date": "2026-11-27", "label": "27五", "count": 3400},
        ]
        large = render_daily_trend_chart(large_rows, avg=7950.0)
        self.assertIn("15,000", large)
        self.assertIn("12,500", large)
        self.assertIn("峰值 26日 (12,500)", large)
        _assert_no_bad_numbers(self, large)

    def test_render_hourly_bar_chart_spacing_peak_and_empty(self):
        empty = render_hourly_bar_chart([])
        self.assertIn('viewBox="0 0 280 148"', empty)
        self.assertIn("暂无记录", empty)
        _assert_no_bad_numbers(self, empty)

        all_zero = render_hourly_bar_chart(
            [{"hour": h, "label": f"{h:02d}", "count": 0} for h in range(8, 17)]
        )
        self.assertIn("暂无记录", all_zero)
        self.assertNotIn("svg-bar--peak", all_zero)
        _assert_no_bad_numbers(self, all_zero)

        nine_hours = [
            {"hour": 8, "label": "08", "count": 2},
            {"hour": 9, "label": "09", "count": 5},
            {"hour": 10, "label": "10", "count": 8},
            {"hour": 11, "label": "11", "count": 7},
            {"hour": 12, "label": "12", "count": 3},
            {"hour": 13, "label": "13", "count": 4},
            {"hour": 14, "label": "14", "count": 7},
            {"hour": 15, "label": "15", "count": 6},
            {"hour": 16, "label": "16", "count": 5},
        ]
        out9 = render_hourly_bar_chart(nine_hours)
        self.assertIn('class="svg-bar--peak"', out9)
        self.assertIn("高峰 10:00 (8)", out9)
        self.assertIn("次峰 11:00 (7)", out9)
        self.assertIn(">10</text>", out9)
        self.assertIn(">5</text>", out9)
        self.assertIn(">09</text>", out9)
        _assert_no_bad_numbers(self, out9)

        # >14 buckets skips odd X labels so labels never collide
        full_day = [{"hour": h, "label": f"{h:02d}", "count": (h % 5)} for h in range(24)]
        out24 = render_hourly_bar_chart(full_day)
        self.assertIn('y="136" text-anchor="middle">00</text>', out24)
        self.assertIn('y="136" text-anchor="middle">02</text>', out24)
        self.assertNotIn('y="136" text-anchor="middle">01</text>', out24)
        self.assertNotIn('y="136" text-anchor="middle">03</text>', out24)
        _assert_no_bad_numbers(self, out24)

    def test_render_grouped_pace_chart_empty_normal_and_dense(self):
        empty = render_grouped_pace_chart([], total_created=0, total_closed=0)
        self.assertIn('viewBox="0 0 420 104"', empty)
        self.assertIn("暂无记录", empty)
        _assert_no_bad_numbers(self, empty)

        pace = [
            {"hour": 9, "label": "09:00", "created": 1, "closed": 1},
            {"hour": 10, "label": "10:00", "created": 2, "closed": 2},
            {"hour": 14, "label": "14:00", "created": 4, "closed": 2},
            {"hour": 15, "label": "15:00", "created": 4, "closed": 2},
        ]
        out = render_grouped_pace_chart(pace, total_created=11, total_closed=7)
        self.assertIn('class="svg-bar--secondary"', out)
        self.assertIn('class="svg-bar--solid"', out)
        self.assertIn("入队 11", out)
        self.assertIn("已结 7", out)
        self.assertIn("14–15h 为入队高峰", out)
        self.assertIn(">4</text>", out)
        _assert_no_bad_numbers(self, out)

        dense = [{"hour": h, "label": f"{h:02d}:00", "created": 1, "closed": 1} for h in range(20)]
        out_dense = render_grouped_pace_chart(dense, total_created=20, total_closed=20)
        self.assertIn(">00:00</text>", out_dense)
        self.assertNotIn(">01:00</text>", out_dense)
        _assert_no_bad_numbers(self, out_dense)

    def test_render_admin_overview_populated_and_fallback_and_xss(self):
        populated_analytics = {
            "snapshot_at_utc8": "2026-11-27 16:20",
            "date_range_label": "11/21–11/27",
            "daily_inbound": [
                {"date": "2026-11-26", "label": "26四", "count": 55},
                {"date": "2026-11-27", "label": "27五", "count": 47},
            ],
            "inbound_7d_total": 1316,
            "inbound_7d_avg": 188.0,
            "inbound_today_total": 47,
            "inbound_all_total": 5432,
            "hourly_inbound_today": [
                {"hour": 10, "label": "10", "count": 8},
                {"hour": 14, "label": "14", "count": 7},
            ],
            "replies_today": {"sent": 34, "shadow": 7, "failed": 2, "total": 43},
            "replies_all": {"sent": 500, "shadow": 120, "failed": 5, "total": 625},
            "filter_7d": {
                "question": 220,
                "feedback": 38,
                "invalid": 31,
                "unsupported": 24,
                "emergency": 3,
                "gray_enroll": 4,
                "total_non_gray": 316,
            },
            "routing_7d": {
                "tier2_rag": 91,
                "tier1_preset": 84,
                "clarify": 27,
                "staff_fallback": 18,
                "other": 0,
                "total": 220,
                "auto_rate_pct": 91.8,
            },
            "batches": {"open": 12, "processing": 6, "queued": 3, "flushed": 100, "active_total": 21},
            "open_cases": {"total": 7, "emergency": 1, "staff_todo": 6},
            "recent_alerts": [
                {
                    "time_hm": "16:04",
                    "datetime": "2026-11-27T16:04",
                    "kind": "reply_failed",
                    "badge": "发信失败",
                    "text": '<script>alert("xss")</script> & 失败',
                },
            ],
        }
        html_out = render_admin_overview(
            populated_analytics,
            reply_mode_text='自动回复已关闭 <img src=x onerror=alert(1)>',
        )
        # Pure fragment: no outer document wrapper
        self.assertNotIn("<html", html_out.lower())
        self.assertNotIn("<body", html_out.lower())
        self.assertNotIn("<script", html_out.lower())
        # Semantic strings & XSS escaping
        self.assertIn("微信接口已受理", html_out)
        self.assertIn("影子模式", html_out)
        self.assertIn("metric-delta--warn", html_out)
        self.assertIn("含 1 紧急", html_out)
        self.assertIn("1,316", html_out)
        self.assertIn("5,432", html_out)
        self.assertIn("自动 91.8%", html_out)
        self.assertIn("16:20 快照", html_out)
        self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt; &amp; 失败", html_out)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html_out)
        _assert_no_bad_numbers(self, html_out)

        # Fallback when today replies == 0 uses all-time replies with "累计" chip
        fallback_analytics = dict(populated_analytics)
        fallback_analytics["replies_today"] = {"sent": 0, "shadow": 0, "failed": 0, "total": 0}
        fallback_analytics["open_cases"] = {"total": 0, "emergency": 0, "staff_todo": 0}
        fallback_out = render_admin_overview(fallback_analytics, reply_mode_text="影子模式运行中")
        self.assertIn('<span class="period-chip">累计</span>', fallback_out)
        self.assertIn("625", fallback_out)
        self.assertIn("累计回复", fallback_out)
        self.assertNotIn("metric-delta--warn", fallback_out)
        _assert_no_bad_numbers(self, fallback_out)

        # Completely empty analytics dict must not crash
        empty_out = render_admin_overview({}, reply_mode_text="")
        self.assertIn("metric-strip", empty_out)
        self.assertIn("dashboard-grid", empty_out)
        self.assertIn("微信接口已受理", empty_out)
        self.assertIn("影子模式", empty_out)
        self.assertIn("暂无记录", empty_out)
        _assert_no_bad_numbers(self, empty_out)

    def test_xss_escaping_across_all_chart_primitives(self):
        payload = '"><script>alert(1)</script>'
        donut_out = render_donut(
            [(payload, "c-green", "var(--green)", 5)],
            center_num=payload,
            center_sub=payload,
            aria_label=payload,
        )
        self.assertNotIn("<script>", donut_out)
        self.assertIn("&lt;script&gt;", donut_out)

        hbar_out = render_stack_hbar_list([(payload, "c-green", 3)], aria_label=payload)
        self.assertNotIn("<script>", hbar_out)
        self.assertIn("&lt;script&gt;", hbar_out)

        trend_out = render_daily_trend_chart([{"date": "2026-11-27", "label": payload, "count": 5}], avg=5.0)
        self.assertNotIn("<script>", trend_out)
        self.assertIn("&lt;script&gt;", trend_out)

        hourly_out = render_hourly_bar_chart([{"hour": 10, "label": payload, "count": 4}])
        self.assertNotIn("<script>", hourly_out)
        self.assertIn("&lt;script&gt;", hourly_out)

        pace_out = render_grouped_pace_chart(
            [{"hour": 10, "label": payload, "created": 2, "closed": 1}],
            total_created=2,
            total_closed=1,
        )
        self.assertNotIn("<script>", pace_out)
        self.assertIn("&lt;script&gt;", pace_out)

    def test_all_svgs_are_well_formed_xml_and_tiny_donut_slice_stays_visible(self):
        tiny_donut = render_donut(
            [
                ("实际发送", "c-green", "var(--green)", 10000),
                ("发送失败", "c-rust", "var(--rust)", 1),
            ],
            center_num="10,001",
            center_sub="累计回复",
            aria_label="大数与极小切片",
        )
        # Tiny slice keeps readable minimum stroke length (>0, not 0.0)
        dashes = [float(m) for m in re.findall(r'stroke-dasharray="([0-9.]+)\s', tiny_donut)]
        self.assertEqual(len(dashes), 2)
        self.assertGreaterEqual(min(dashes), 2.5)
        _assert_no_bad_numbers(self, tiny_donut)

        samples = [
            render_sparkline([]),
            render_sparkline([0, 0, 0]),
            render_sparkline([1, 5, 3, 9]),
            tiny_donut,
            render_donut([], center_num="0", center_sub="暂无记录", aria_label="空"),
            render_daily_trend_chart([], avg=0.0),
            render_daily_trend_chart([{"date": "2026-11-27", "label": "27五", "count": 5}], avg=5.0),
            render_hourly_bar_chart([]),
            render_hourly_bar_chart([{"hour": h, "label": f"{h:02d}", "count": h} for h in range(8, 24)]),
            render_grouped_pace_chart([], total_created=0, total_closed=0),
            render_grouped_pace_chart(
                [{"hour": 9, "label": "09:00", "created": 3, "closed": 2}],
                total_created=3,
                total_closed=2,
            ),
            render_admin_overview({}, reply_mode_text=""),
        ]
        for markup in samples:
            svgs = re.findall(r"<svg\b.*?</svg>", markup, flags=re.DOTALL)
            self.assertTrue(svgs, f"expected at least one <svg> in: {markup[:120]}")
            for svg in svgs:
                root = ET.fromstring(svg)
                self.assertEqual(root.tag, "svg")


if __name__ == "__main__":
    unittest.main()
