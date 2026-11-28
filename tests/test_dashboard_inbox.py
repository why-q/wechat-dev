from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from support_backend.dashboard_inbox import render_inbox
from support_backend.db import SupportStore
from support_backend.filters import classify_message


class InboxProjectionTests(unittest.TestCase):
    def test_projects_reader_messages_and_pages_by_sequence(self):
        now = [1_800_000_000.0]
        with tempfile.TemporaryDirectory() as temp:
            store = SupportStore(Path(temp) / "inbox.db", clock=lambda: now[0])
            try:
                seqs = []
                private_id = "o_private_<unique>&"
                for index in range(52):
                    content = "<reader> & private"
                    payload = {
                        "transport": "cloud", "msg_type": "text", "event": "",
                        "msg_id": str(index), "from_user": private_id, "to_user": "library",
                        "content": content, "create_time": "1800000000",
                    }
                    result = store.ingest_event(payload, classify_message(msg_type="text", content=content), client_ip="")
                    seqs.append(result.seq)
                callback = {"transport": "cloud", "msg_type": "event", "event": "subscribe", "from_user": "reader", "to_user": "library", "content": "", "create_time": "1800000000"}
                store.ingest_event(callback, classify_message(msg_type="event", content="", event="subscribe"), client_ip="")
                no_sender = {"transport": "cloud", "msg_type": "text", "event": "", "from_user": "", "to_user": "library", "content": "synthetic", "create_time": "1800000000"}
                store.ingest_event(no_sender, classify_message(msg_type="text", content="synthetic"), client_ip="")
                first = render_inbox(store)
                self.assertEqual(first.count('class="case-state"'), 50)
                self.assertIn("更早私信", first)
                self.assertIn("&lt;reader&gt; &amp; private", first)
                self.assertNotIn(private_id, first)
                self.assertNotIn("o_private_&lt;unique&gt;&amp;", first)
                older = render_inbox(store, before=seqs[2])
                self.assertEqual(older.count('class="case-state"'), 2)
                self.assertNotIn("更早私信", older)
                self.assertIn("返回最新", older)
                now[0] += 8 * 24 * 3600
                self.assertIn("当前没有保留中的读者私信", render_inbox(store))
            finally:
                store.close()

    def test_projects_mixed_message_kinds_without_exposing_gray_keyword(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SupportStore(Path(temp) / "mixed.db", clock=lambda: 1_800_000_000.0)
            try:
                keyword = "PRIVATE_ENROLL_TOKEN_73"
                samples = [
                    ("text", "有人晕倒", "emergency"),
                    ("image", "", "unsupported"),
                    ("text", "https://example.invalid/path", "unsupported"),
                    ("text", "建议增加自习室座位", "feedback"),
                    ("text", keyword, "gray_enroll"),
                    ("text", "<b>markup & text</b>", "question"),
                ]
                for index, (kind, text, expected) in enumerate(samples):
                    payload = {
                        "transport": "cloud", "msg_type": kind, "event": "",
                        "msg_id": f"mixed-{index}", "from_user": f"reader-{index}",
                        "to_user": "library", "content": text, "create_time": "1800000000",
                    }
                    decision = classify_message(msg_type=kind, content=text, gray_enroll_keyword=keyword)
                    self.assertEqual(decision.category, expected)
                    store.ingest_event(payload, decision, client_ip="")
                rendered = render_inbox(store)
                self.assertIn("紧急消息", rendered)
                self.assertIn("未结案", rendered)
                self.assertIn("图片消息", rendered)
                self.assertIn("https://example.invalid/path", rendered)
                self.assertIn("建议增加自习室座位", rendered)
                self.assertIn("灰度入组消息", rendered)
                self.assertNotIn("文本消息", rendered)
                self.assertNotIn(keyword, rendered)
                self.assertIn("&lt;b&gt;markup &amp; text&lt;/b&gt;", rendered)
                self.assertNotIn("<b>markup & text</b>", rendered)
            finally:
                store.close()

    def test_can_reply_shows_eligible_form_staff_log_and_preset_checkbox(self):
        now = [1_800_000_000.0]
        with tempfile.TemporaryDirectory() as temp:
            store = SupportStore(Path(temp) / "reply_inbox.db", clock=lambda: now[0])
            try:
                secret_reader = "o_secret_inbox_reader_42"
                payload = {
                    "transport": "cloud",
                    "msg_type": "text",
                    "event": "",
                    "msg_id": "reply-1",
                    "from_user": secret_reader,
                    "to_user": "library",
                    "content": "请问开馆时间是几点？",
                    "create_time": "1800000000",
                }
                res = store.ingest_event(
                    payload,
                    classify_message(msg_type="text", content=payload["content"]),
                    client_ip="",
                )
                store.record_outbound_reply(
                    account_id="library",
                    reader_id=secret_reader,
                    trigger_kind="staff_dashboard_reply",
                    trigger_ref_id=res.seq,
                    content="每天 8:00-22:00 开放。<script>alert(1)</script>",
                    delivery_mode="live",
                    status="sent",
                    operator_username="alice<b>",
                    now_epoch=now[0] + 60,
                )
                now[0] += 3600  # 1 hour elapsed -> 47h left, 1/5 sent

                rendered = render_inbox(
                    store,
                    can_reply=True,
                    can_draft_preset=True,
                    active_view="inbox",
                    show_closed=True,
                )
                self.assertIn('class="case-row case-row--compact"', rendered)
                self.assertIn('class="inbox-reply-log"', rendered)
                self.assertIn("已回复（alice&lt;b&gt;", rendered)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
                self.assertIn("回复（剩47h·1/5）", rendered)
                self.assertIn(f'action="/dashboard/librarian/inbox/{res.seq}/reply"', rendered)
                self.assertIn('<input type="hidden" name="view" value="inbox">', rendered)
                self.assertIn('<input type="hidden" name="show" value="all">', rendered)
                self.assertIn('name="reply_text"', rendered)
                self.assertNotIn('name="save_preset_draft"', rendered)
                self.assertIn("后台自动扫描是否可沉淀为 1. 标准模板", rendered)
                self.assertIn("发送微信回复", rendered)
                self.assertNotIn("文本消息", rendered)
                self.assertNotIn(secret_reader, rendered)
                self.assertNotIn("<script>", rendered)
                self.assertIsNone(re.search(r"\bon[a-z]+\s*=", rendered, re.IGNORECASE))

                no_draft = render_inbox(store, can_reply=True, can_draft_preset=False)
                self.assertNotIn('name="save_preset_draft"', no_draft)
                self.assertIn('<input type="hidden" name="show" value="open">', no_draft)
            finally:
                store.close()

    def test_can_reply_blocks_expired_window_and_exhausted_quota(self):
        now = [1_800_000_000.0]
        with tempfile.TemporaryDirectory() as temp:
            store = SupportStore(Path(temp) / "blocked_inbox.db", clock=lambda: now[0])
            try:
                # Reader A: older than 48h
                res_old = store.ingest_event(
                    {
                        "transport": "cloud",
                        "msg_type": "text",
                        "event": "",
                        "msg_id": "old-1",
                        "from_user": "o_secret_old_reader",
                        "to_user": "library",
                        "content": "两天前的留言",
                        "create_time": "1800000000",
                    },
                    classify_message(msg_type="text", content="两天前的留言"),
                    client_ip="",
                )
                now[0] += 49 * 3600  # 49h later -> Reader A expired
                # Reader B: fresh message but 5 sent replies -> quota exhausted
                res_full = store.ingest_event(
                    {
                        "transport": "cloud",
                        "msg_type": "text",
                        "event": "",
                        "msg_id": "full-1",
                        "from_user": "o_secret_full_reader",
                        "to_user": "library",
                        "content": "刚刚的留言",
                        "create_time": str(int(now[0])),
                    },
                    classify_message(msg_type="text", content="刚刚的留言"),
                    client_ip="",
                )
                for idx in range(5):
                    store.record_outbound_reply(
                        account_id="library",
                        reader_id="o_secret_full_reader",
                        trigger_kind="staff_dashboard_reply",
                        trigger_ref_id=res_full.seq,
                        content=f"回复 #{idx + 1}",
                        delivery_mode="live",
                        status="sent",
                        operator_username="bob",
                        now_epoch=now[0] + idx + 1,
                    )

                rendered = render_inbox(store, can_reply=True)
                self.assertIn('<span class="inbox-quota-blocked">超48h</span>', rendered)
                self.assertIn('<span class="inbox-quota-blocked">额度满</span>', rendered)
                self.assertIn("已超 48 小时回复窗口或已达 5 条上限，请前往微信公众号后台回复。", rendered)
                self.assertNotIn(f'action="/dashboard/librarian/inbox/{res_old.seq}/reply"', rendered)
                self.assertNotIn(f'action="/dashboard/librarian/inbox/{res_full.seq}/reply"', rendered)
                self.assertNotIn("o_secret_", rendered)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
