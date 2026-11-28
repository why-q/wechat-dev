from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
