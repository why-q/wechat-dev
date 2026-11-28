from __future__ import annotations

import re
import unittest

from support_backend.dashboard_librarian import render_librarian_workbench


def _sample_analytics() -> dict:
    return {
        "snapshot_at_utc8": "2026-11-28 16:20",
        "open_emergency": [
            {
                "case_id": 101,
                "case_kind": "emergency",
                "summary": "电梯内有人受困",
                "latest_message": "三楼东侧电梯突然停电困住两位读者",
                "created_at_utc8": "2026-11-28 16:08",
                "created_hm": "16:08",
                "wait_minutes": 12,
                "reader_id": "o_secret_emg_openid_999",
                "messages": [
                    {
                        "sender_role": "reader",
                        "content": "三楼东侧电梯突然停电困住两位读者",
                        "created_hm": "16:08",
                    }
                ],
            }
        ],
        "open_todo": [
            {
                "case_id": 201,
                "case_kind": "staff_todo",
                "summary": "预约书到馆后如何领取",
                "latest_message": "收到到馆通知后，可以请同事代取吗？",
                "created_at_utc8": "2026-11-28 14:19",
                "created_hm": "14:19",
                "wait_minutes": 121,
                "reader_id": "o_secret_todo_openid_201",
                "messages": [
                    {
                        "sender_role": "reader",
                        "content": "收到到馆通知后，可以请同事代取吗？",
                        "created_hm": "14:19",
                    },
                    {
                        "sender_role": "staff",
                        "content": "请携带双方证件到总服务台。",
                        "created_hm": "14:25",
                    },
                ],
            },
            {
                "case_id": 202,
                "case_kind": "staff_todo",
                "summary": "馆际互借到书通知",
                "latest_message": "请问馆际互借的书到了怎么通知？",
                "created_at_utc8": "2026-11-28 15:37",
                "created_hm": "15:37",
                "wait_minutes": 43,
                "messages": [
                    {
                        "sender_role": "reader",
                        "content": "请问馆际互借的书到了怎么通知？",
                        "created_hm": "15:37",
                    }
                ],
            },
            {
                "case_id": 203,
                "case_kind": "staff_todo",
                "summary": "借阅证补办材料",
                "latest_message": "学生证丢了补办要什么？",
                "created_at_utc8": "2026-11-28 16:14",
                "created_hm": "16:14",
                "wait_minutes": 6,
                "messages": [],
            },
        ],
        "open_counts": {"total": 4, "emergency": 1, "staff_todo": 3},
        "wait_buckets": {"emergency": 1, "over_60m": 1, "m30_60": 1, "under_30m": 1},
        "longest_wait": {"minutes": 121, "created_hm": "14:19", "summary": "预约书到馆后如何领取"},
        "today_stats": {"created": 19, "closed": 12, "close_rate_pct": 63.2},
        "response_stats": {
            "avg_minutes": 24,
            "recent_durations": [18, 22, 15, 29, 31, 24],
            "sample_count": 6,
            "scope": "recent",
        },
        "latency_buckets": {
            "le_10m": 3,
            "m10_20": 4,
            "m20_30": 3,
            "gt_30m": 2,
            "total": 12,
            "within_30m_pct": 83,
        },
        "hourly_pace_today": [
            {"hour": 9, "label": "09:00", "created": 1, "closed": 1},
            {"hour": 14, "label": "14:00", "created": 4, "closed": 2},
            {"hour": 16, "label": "16:00", "created": 3, "closed": 1},
        ],
        "recent_closed": [
            {
                "case_id": 300 + i,
                "case_kind": "staff_todo",
                "summary": f"已结咨询 #{i}",
                "closed_at_utc8": f"2026-11-28 15:{10 + i:02d}",
                "resolution_note": f"已在公众号回复 #{i}",
                "duration_minutes": 12 + i,
                "reader_id": f"o_secret_closed_{i}",
            }
            for i in range(8)
        ],
        "recent_feedbacks": [
            {
                "feedback_id": 501,
                "created_at_utc8": "2026-11-28 15:12 UTC+8",
                "content": "建议三楼自习室增加插座",
                "status": "open",
                "staff_note": "已转物业评估",
                "reader_id": "o_secret_fb_openid_501",
            }
        ],
    }


class LibrarianWorkbenchTests(unittest.TestCase):
    def test_populated_render_all_sections_and_forms(self):
        inbox_html = (
            '<section class="panel queue-panel"><h2>私信按未结案展示</h2>'
            '<ul class="case-list"><li class="case-row">保留私信内容</li></ul></section>'
        )
        html_out = render_librarian_workbench(
            _sample_analytics(),
            inbox_html=inbox_html,
            active_view="all",
            notice="工单 #201 已结案。",
        )

        # View tabs + active marker + counts
        self.assertIn('class="view-tabs"', html_out)
        self.assertIn('href="/dashboard/librarian" aria-current="page"', html_out)
        self.assertIn('href="/dashboard/librarian?view=cases"', html_out)
        self.assertIn('href="/dashboard/librarian?view=inbox"', html_out)
        self.assertIn('href="/dashboard/librarian?view=feedback"', html_out)

        # Notice banner
        self.assertIn('<p class="form-notice" role="status">工单 #201 已结案。</p>', html_out)

        # 4 KPI strip
        self.assertIn("待跟进", html_out)
        self.assertIn("1 紧急", html_out)
        self.assertIn("最长等待", html_out)
        self.assertIn("121", html_out)
        self.assertIn("今日已结", html_out)
        self.assertIn("今日累计入队 19 项", html_out)
        self.assertIn("均响耗时", html_out)
        self.assertIn("近 6 笔已结均值", html_out)
        self.assertIn('class="metric-spark"', html_out)

        # Urgent banner + close form
        self.assertIn('class="urgent-banner"', html_out)
        self.assertIn("电梯内有人受困", html_out)
        self.assertIn("已发紧急联系方式", html_out)
        self.assertIn('action="/dashboard/librarian/cases/101/close"', html_out)

        # Open todo queue + severity classes + messages + close forms
        self.assertIn("普通未结案队列", html_out)
        self.assertIn("按等待时长降序 · 3 条", html_out)
        self.assertIn("预约书到馆后如何领取", html_out)
        self.assertIn("wait-label--high", html_out)
        self.assertIn("c-rust", html_out)
        self.assertIn("wait-label--mid", html_out)
        self.assertIn("c-ochre", html_out)
        self.assertIn("c-green", html_out)
        self.assertIn("优先", html_out)
        self.assertIn("读者 14:19：", html_out)
        self.assertIn("馆员 14:25：", html_out)
        self.assertIn('action="/dashboard/librarian/cases/201/close"', html_out)
        self.assertIn('action="/dashboard/librarian/cases/202/close"', html_out)
        self.assertIn('action="/dashboard/librarian/cases/203/close"', html_out)
        self.assertIn('<input type="hidden" name="view" value="all">', html_out)
        self.assertIn('name="resolution_note"', html_out)
        self.assertIn('<input type="checkbox" name="confirm" value="yes" required>', html_out)
        self.assertEqual(html_out.count("标记结案"), 4)

        # Right stack: donut + recent closed (capped at 6)
        self.assertIn("待办结构", html_out)
        self.assertIn('class="donut-svg donut-svg--sm"', html_out)
        self.assertIn("最近已结工单", html_out)
        self.assertIn("已结咨询 #0", html_out)
        self.assertIn("已结咨询 #5", html_out)
        self.assertNotIn("已结咨询 #6", html_out)

        # Charts row
        self.assertIn("今日入队 vs 已结", html_out)
        self.assertIn("已结响应耗时分布", html_out)
        self.assertIn("83% 在 30 分钟目标内结案", html_out)

        # Feedback panel
        self.assertIn("意见反馈", html_out)
        self.assertIn("建议三楼自习室增加插座", html_out)
        self.assertIn("已转物业评估", html_out)

        # Retained inbox HTML preserved verbatim inside wrapper
        self.assertIn(f'<div id="inbox" class="panel--inbox-split">{inbox_html}</div>', html_out)

        # Privacy & security: no reader_id / OpenID, no script or inline event handlers
        self.assertNotIn("o_secret_", html_out)
        self.assertNotIn("<script", html_out.lower())
        self.assertIsNone(re.search(r"\bon[a-z]+\s*=", html_out, re.IGNORECASE))

    def test_empty_analytics_renders_gracefully(self):
        inbox_html = '<section class="panel queue-panel"><p>当前没有保留中的读者私信。</p></section>'
        html_out = render_librarian_workbench({}, inbox_html=inbox_html)

        self.assertIn("待跟进", html_out)
        self.assertIn("无紧急", html_out)
        self.assertIn("—", html_out)
        self.assertIn("当前没有未结普通待办。", html_out)
        self.assertIn("暂无已结工单", html_out)
        self.assertIn("今日暂无工单入队或结案记录。", html_out)
        self.assertIn("暂无已结耗时分布数据。", html_out)
        self.assertIn("暂无意见反馈。", html_out)
        self.assertNotIn('class="urgent-banner"', html_out)
        self.assertNotIn('class="form-notice"', html_out)
        self.assertIn(inbox_html, html_out)

    def test_xss_payloads_are_escaped_everywhere(self):
        xss = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
        analytics = {
            "open_emergency": [
                {
                    "case_id": 11,
                    "case_kind": "emergency",
                    "summary": xss,
                    "created_hm": "10:00",
                    "wait_minutes": 5,
                    "messages": [{"sender_role": "reader", "content": xss, "created_hm": "10:00"}],
                }
            ],
            "open_todo": [
                {
                    "case_id": 12,
                    "case_kind": "<b>custom</b>",
                    "summary": xss,
                    "created_hm": "09:00",
                    "wait_minutes": 65,
                    "messages": [{"sender_role": "staff", "content": xss, "created_hm": "09:05"}],
                }
            ],
            "open_counts": {"total": 2, "emergency": 1, "staff_todo": 1},
            "longest_wait": {"minutes": 65, "created_hm": "09:00", "summary": xss},
            "recent_closed": [
                {
                    "case_id": 13,
                    "summary": xss,
                    "closed_hm": "08:30",
                    "resolution_note": xss,
                    "duration_minutes": 10,
                }
            ],
            "recent_feedbacks": [
                {
                    "feedback_id": 14,
                    "created_at_utc8": xss,
                    "content": xss,
                    "status": xss,
                    "staff_note": xss,
                }
            ],
        }
        html_out = render_librarian_workbench(
            analytics,
            inbox_html="",
            active_view="all",
            notice=xss,
        )

        self.assertNotIn("<script>", html_out)
        self.assertNotIn("<img ", html_out)
        self.assertNotIn("<b>custom</b>", html_out)
        self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", html_out)
        self.assertIn("&lt;b&gt;custom&lt;/b&gt;", html_out)

    def test_view_filtering_shows_and_hides_sections(self):
        analytics = _sample_analytics()
        inbox_html = '<section class="panel queue-panel">INBOX_SENTINEL</section>'

        cases_html = render_librarian_workbench(analytics, inbox_html=inbox_html, active_view="cases")
        self.assertIn('href="/dashboard/librarian?view=cases" aria-current="page"', cases_html)
        self.assertIn('class="urgent-banner"', cases_html)
        self.assertIn("普通未结案队列", cases_html)
        self.assertIn("今日入队 vs 已结", cases_html)
        self.assertIn('<input type="hidden" name="view" value="cases">', cases_html)
        self.assertNotIn('id="feedback"', cases_html)
        self.assertNotIn("INBOX_SENTINEL", cases_html)

        inbox_view = render_librarian_workbench(analytics, inbox_html=inbox_html, active_view="inbox")
        self.assertIn('href="/dashboard/librarian?view=inbox" aria-current="page"', inbox_view)
        self.assertIn("INBOX_SENTINEL", inbox_view)
        self.assertNotIn('class="urgent-banner"', inbox_view)
        self.assertNotIn("普通未结案队列", inbox_view)
        self.assertNotIn('id="feedback"', inbox_view)

        fb_view = render_librarian_workbench(analytics, inbox_html=inbox_html, active_view="feedback")
        self.assertIn('href="/dashboard/librarian?view=feedback" aria-current="page"', fb_view)
        self.assertIn('id="feedback"', fb_view)
        self.assertIn("建议三楼自习室增加插座", fb_view)
        self.assertNotIn('class="urgent-banner"', fb_view)
        self.assertNotIn("普通未结案队列", fb_view)
        self.assertNotIn("INBOX_SENTINEL", fb_view)

        # Unknown view safely falls back to all
        unknown_view = render_librarian_workbench(analytics, inbox_html=inbox_html, active_view="invalid_tab")
        self.assertIn('href="/dashboard/librarian" aria-current="page"', unknown_view)
        self.assertIn("普通未结案队列", unknown_view)
        self.assertIn('id="feedback"', unknown_view)
        self.assertIn("INBOX_SENTINEL", unknown_view)

    def test_custom_chart_renderer_hooks_are_invoked(self):
        html_out = render_librarian_workbench(
            _sample_analytics(),
            inbox_html="",
            render_micro_bar_fn=lambda segs: f"<div data-micro='{len(segs)}'></div>",
            render_sparkline_fn=lambda vals: f"<div data-spark='{len(vals)}'></div>",
            render_donut_fn=lambda segs, **kw: f"<div data-donut='{kw['center_value']}'></div>",
            render_pace_fn=lambda rows: f"<div data-pace='{len(rows)}'></div>",
            render_hbar_fn=lambda items, **kw: f"<div data-hbar='{len(items)}'></div>",
        )
        self.assertIn("<div data-micro='2'></div>", html_out)
        self.assertIn("<div data-spark='6'></div>", html_out)
        self.assertIn("<div data-donut='4'></div>", html_out)
        self.assertIn("<div data-pace='3'></div>", html_out)
        self.assertIn("<div data-hbar='4'></div>", html_out)

    def test_show_closed_toggle_and_closed_cases_direct_reply(self):
        analytics = _sample_analytics()
        analytics["closed_cases"] = [
            {
                "case_id": 400,
                "case_kind": "staff_todo",
                "status": "replied",
                "summary": "已处理工单-静音舱预约咨询",
                "closed_at_utc8": "2026-11-28 15:45",
                "closed_by": "",
                "resolution_note": "",
                "reader_id": "o_secret_replied_case_400",
                "messages": [
                    {"sender_role": "reader", "content": "静音舱怎么预约？", "created_hm": "15:40"},
                    {
                        "sender_role": "staff",
                        "operator_username": "zhangsan",
                        "content": "可在预约系统提前一天预约。",
                        "created_hm": "15:45",
                    },
                ],
                "reply_quota": {
                    "can_reply": True,
                    "window_open": True,
                    "window_remaining_seconds": 12 * 3600,
                    "sent_in_window": 1,
                    "quota_limit": 5,
                    "blocked_reason": "",
                },
            },
            {
                "case_id": 401,
                "case_kind": "staff_todo",
                "status": "closed",
                "summary": "已结案工单-借阅权限咨询",
                "closed_at_utc8": "2026-11-28 15:40",
                "closed_by": "zhangsan",
                "resolution_note": "已电话回访并更新借阅权限",
                "reader_id": "o_secret_closed_case_401",
                "messages": [
                    {"sender_role": "reader", "content": "我的借阅权限怎么受限了？", "created_hm": "15:20"},
                    {
                        "sender_role": "staff",
                        "operator_username": "zhangsan",
                        "content": "已为您恢复借阅权限。",
                        "created_hm": "15:35",
                    },
                ],
                "reply_quota": {
                    "can_reply": True,
                    "window_open": True,
                    "window_remaining_seconds": 12 * 3600,
                    "sent_in_window": 1,
                    "quota_limit": 5,
                    "blocked_reason": "",
                },
            },
        ]

        # Default show_closed=False hides closed_cases section
        open_only = render_librarian_workbench(analytics, inbox_html="", show_closed=False)
        self.assertIn('class="case-filter-toggle"', open_only)
        self.assertIn('href="/dashboard/librarian" aria-current="true">仅看未结案</a>', open_only)
        self.assertIn('href="/dashboard/librarian?show=all">显示全部工单（含已结）</a>', open_only)
        self.assertNotIn("已结案工单-借阅权限咨询", open_only)
        self.assertNotIn('action="/dashboard/librarian/cases/401/reply"', open_only)

        # show_closed=True shows replied & closed cases with distinct badges and single reply button
        with_closed = render_librarian_workbench(
            analytics,
            inbox_html="",
            active_view="cases",
            show_closed=True,
            can_reply=True,
            can_close=True,
            can_draft_preset=True,
        )
        self.assertIn('href="/dashboard/librarian?view=cases">仅看未结案</a>', with_closed)
        self.assertIn('href="/dashboard/librarian?view=cases&show=all" aria-current="true">显示全部工单（含已结）</a>', with_closed)
        self.assertIn("已处理工单-静音舱预约咨询", with_closed)
        self.assertIn('<span class="badge good">已处理 · 等待读者</span>', with_closed)
        self.assertIn("已结案工单-借阅权限咨询", with_closed)
        self.assertIn('<span class="badge muted">已结案</span>', with_closed)
        self.assertIn("2026-11-28 15:40", with_closed)
        self.assertIn("zhangsan", with_closed)
        self.assertIn("馆员 (zhangsan) 15:35：", with_closed)
        self.assertIn("可直复 · 剩 12h · 1/5", with_closed)
        self.assertIn('action="/dashboard/librarian/cases/401/reply"', with_closed)
        self.assertIn('action="/dashboard/librarian/cases/401/draft-preset"', with_closed)
        self.assertIn("生成模板草稿", with_closed)
        self.assertIn('<input type="hidden" name="show" value="all">', with_closed)
        self.assertNotIn('name="save_preset_draft"', with_closed)
        self.assertIn("发送后标记为已处理，后台自动扫描是否可沉淀为 1. 标准模板", with_closed)
        # Closed case must NOT have a close form
        self.assertNotIn('action="/dashboard/librarian/cases/401/close"', with_closed)
        # Open cases DO have single merged reply button (no reply_and_close)
        self.assertIn('action="/dashboard/librarian/cases/101/reply"', with_closed)
        self.assertIn('action="/dashboard/librarian/cases/201/reply"', with_closed)
        self.assertNotIn('value="reply_and_close"', with_closed)
        self.assertIn(">发送回复</button>", with_closed)
        self.assertNotIn("o_secret_", with_closed)

    def test_permissions_hide_reply_close_and_draft_controls(self):
        analytics = _sample_analytics()

        no_reply = render_librarian_workbench(analytics, inbox_html="", can_reply=False, can_close=True)
        self.assertNotIn('class="reply-form"', no_reply)
        self.assertNotIn('action="/dashboard/librarian/cases/201/reply"', no_reply)
        self.assertIn('action="/dashboard/librarian/cases/201/close"', no_reply)

        no_close = render_librarian_workbench(analytics, inbox_html="", can_reply=True, can_close=False)
        self.assertIn('action="/dashboard/librarian/cases/201/reply"', no_close)
        self.assertNotIn('action="/dashboard/librarian/cases/201/close"', no_close)
        self.assertNotIn('value="reply_and_close"', no_close)
        self.assertNotIn("标记结案", no_close)

        no_draft = render_librarian_workbench(analytics, inbox_html="", can_reply=True, can_draft_preset=False)
        self.assertNotIn('name="save_preset_draft"', no_draft)

    def test_blocked_quota_and_error_notice_and_xss_in_new_fields(self):
        xss = '<script>alert("x")</script><img src=x onerror=alert(2)>'
        analytics = _sample_analytics()
        analytics["open_todo"][0]["reply_quota"] = {
            "can_reply": False,
            "window_open": False,
            "window_remaining_seconds": 0,
            "sent_in_window": 2,
            "quota_limit": 5,
            "blocked_reason": "window_expired",
        }
        analytics["open_todo"][1]["reply_quota"] = {
            "can_reply": False,
            "window_open": True,
            "window_remaining_seconds": 1800,
            "sent_in_window": 5,
            "quota_limit": 5,
            "blocked_reason": "quota_exhausted",
        }
        analytics["closed_cases"] = [
            {
                "case_id": 499,
                "status": "closed",
                "summary": xss,
                "closed_at_utc8": xss,
                "closed_by": xss,
                "resolution_note": xss,
                "reader_id": "o_secret_xss_499",
                "messages": [
                    {
                        "sender_role": "staff",
                        "operator_username": xss,
                        "content": xss,
                        "created_hm": "16:00",
                    }
                ],
                "reply_quota": {
                    "can_reply": True,
                    "window_open": True,
                    "window_remaining_seconds": 900,
                    "sent_in_window": 0,
                    "quota_limit": 5,
                    "blocked_reason": "",
                },
            }
        ]

        html_out = render_librarian_workbench(
            analytics,
            inbox_html="",
            notice=f"发送失败：{xss}",
            notice_kind="error",
            show_closed=True,
            can_reply=True,
            can_draft_preset=True,
        )
        self.assertIn('class="form-notice form-notice--error"', html_out)
        self.assertIn("超48h窗口 · 2/5", html_out)
        self.assertIn("额度已满 · 5/5", html_out)
        self.assertIn("已超 48 小时回复窗口或已达 5 条额度，请前往微信公众号后台回复。", html_out)
        self.assertNotIn('action="/dashboard/librarian/cases/201/reply"', html_out)
        self.assertNotIn('action="/dashboard/librarian/cases/202/reply"', html_out)
        # Case 203 has no reply_quota -> graceful fallback allows form
        self.assertIn('action="/dashboard/librarian/cases/203/reply"', html_out)
        # Case 499 has 900s = 15m remaining
        self.assertIn("可直复 · 剩 15m · 0/5", html_out)
        self.assertNotIn("o_secret_", html_out)
        self.assertNotIn("<script>", html_out)
        self.assertNotIn("<img ", html_out)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html_out)
        self.assertIsNone(re.search(r"<[^>]*\bon[a-z]+\s*=", html_out, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
