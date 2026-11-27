"""Tests for Step 2 Filter rules, SQLite merge store, recovery, and relay support APIs."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WECHAT_TOKEN", "test-only-token")

import relay_receiver
from support_backend import (
    EMERGENCY_CONTACT_REPLY,
    FEEDBACK_RECEIVED_REPLY,
    GRAY_ENROLL_ADDED_REPLY,
    GRAY_ENROLL_ALREADY_REPLY,
    PresetCatalog,
    SupportPipeline,
    SupportResponder,
    SupportStore,
    classify_message,
)
from support_backend.db import SCHEMA_VERSION


async def _async_asgi_request(
    application,
    method: str,
    path: str,
    *,
    query: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("latin-1"),
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
        "state": {},
    }
    delivered = False
    status_code = 500
    chunks: list[bytes] = []

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await application(scope, receive, send)
    return status_code, b"".join(chunks).decode("utf-8")


class FakeClock:
    def __init__(self, start: float = 1_792_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class FilterClassificationTests(unittest.TestCase):
    def test_unsupported_events_media_and_bare_urls(self):
        view = classify_message("event", event="VIEW")
        self.assertEqual((view.category, view.reason), ("unsupported", "wechat_event:VIEW"))

        image = classify_message("image")
        self.assertEqual((image.category, image.reason), ("unsupported", "media:image"))

        bare_url = classify_message("text", " https://mp.weixin.qq.com/s/test123 ")
        self.assertEqual((bare_url.category, bare_url.reason), ("unsupported", "text_only_url"))

        mixed = classify_message("text", "这个链接打不开 https://www.lib.stu.edu.cn/article-1.html")
        self.assertEqual(mixed.category, "question")

    def test_invalid_noise_and_bare_greetings(self):
        for content in ("", "   ", "？", "...", "[微笑]", "/::)", "你好", "在吗", "hello", "1"):
            decision = classify_message("text", content)
            self.assertEqual(decision.category, "invalid", f"expected invalid for {content!r}")

        greet_with_question = classify_message("text", "你好，请问图书馆几点关门？")
        self.assertEqual(greet_with_question.category, "question")

    def test_emergency_vs_account_or_locker_lock_guards(self):
        trapped = classify_message("text", "我被锁在图书馆了。")
        self.assertEqual(trapped.category, "emergency")

        medical = classify_message("text", "三楼阅览室有人晕倒了")
        self.assertEqual(medical.category, "emergency")

        locker = classify_message("text", "怎么锁定储物柜")
        self.assertEqual(locker.category, "question")

        card_lock = classify_message("text", "我的借阅账号被锁定了怎么办")
        self.assertEqual(card_lock.category, "question")

    def test_feedback_vs_purchase_recommendation_and_complaints(self):
        suggestion = classify_message("text", "建议二楼多加几个电源插座")
        self.assertEqual(suggestion.category, "feedback")

        praise = classify_message("text", "表扬服务台的馆员老师，非常耐心")
        self.assertEqual(praise.category, "feedback")

        recommend_book = classify_message("text", "如何向图书馆推荐购买新书？")
        self.assertEqual(recommend_book.category, "question")

        complaint = classify_message("text", "投诉 国际生在安静区大声喧哗，没人管吗")
        self.assertEqual(complaint.category, "question")

        cooling_q = classify_message("text", "白天图书馆好热啊，能不能调低一点空调？")
        self.assertEqual(cooling_q.category, "question")

    def test_gray_enroll_keyword_classification_and_default_disabled(self):
        test_kw = "灰度测试暗号"
        # Disabled by default when no keyword is configured.
        self.assertEqual(classify_message("text", test_kw).category, "question")
        self.assertEqual(classify_message("text", test_kw, gray_enroll_keyword="").category, "question")

        for text in (test_kw, f"  {test_kw}！ ", f"「{test_kw}」", f"[微笑] {test_kw}。"):
            decision = classify_message("text", text, gray_enroll_keyword=test_kw)
            self.assertEqual(
                (decision.category, decision.reason),
                ("gray_enroll", "gray_allowlist_keyword"),
                f"expected gray_enroll for {text!r}",
            )

        for non_command in (f"请问{test_kw}是什么意思", f"{test_kw}怎么用", "灰度 测试暗号"):
            decision = classify_message("text", non_command, gray_enroll_keyword=test_kw)
            self.assertEqual(decision.category, "question", f"unexpected enroll for {non_command!r}")


class SupportStoreAndMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "state" / "support.db"
        self.clock = FakeClock()
        self.store = SupportStore(
            self.db_path,
            quiet_seconds=3.0,
            max_wait_seconds=10.0,
            merge_enabled=True,
            clock=self.clock,
        )

    def tearDown(self):
        self.store.close()
        self.tmpdir.cleanup()

    def _msg(self, msg_id: str | None, content: str, *, msg_type: str = "text", event: str = "", reader: str = "o_reader_1", account: str = "gh_lib"):
        payload = {
            "transport": "cloud",
            "msg_type": msg_type,
            "event": event,
            "msg_id": msg_id,
            "from_user": reader,
            "to_user": account,
            "content": content,
            "create_time": str(int(self.clock.now)),
        }
        decision = classify_message(msg_type=msg_type, content=content, event=event)
        return payload, self.store.ingest_event(payload, decision, client_ip="127.0.0.1")

    def test_restrictive_permissions_and_account_scoped_dedup(self):
        self.assertEqual(stat.S_IMODE(self.db_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)

        _, first = self._msg("m-1", "续借怎么操作", account="gh_a")
        self.clock.advance(2.0)
        _, dup = self._msg("m-1", "续借怎么操作", account="gh_a")
        _, other_acc = self._msg("m-1", "续借怎么操作", account="gh_b")

        self.assertFalse(first.duplicate)
        self.assertTrue(dup.duplicate)
        self.assertEqual(dup.seq, first.seq)
        # Duplicate retry must NOT reset the 3s quiet timer.
        self.assertEqual(dup.batch.quiet_deadline_epoch, first.batch.quiet_deadline_epoch)
        self.assertFalse(other_acc.duplicate)
        self.assertNotEqual(other_acc.batch.batch_id, first.batch.batch_id)

    def test_quiet_3s_reset_and_10s_hard_cap(self):
        t0 = self.clock.now
        _, r1 = self._msg("q1", "你好，我借的书快到期了")
        self.assertEqual(r1.batch.flush_at_epoch, t0 + 3.0)

        # Arrival at +2s extends flush_at to +5s.
        self.clock.advance(2.0)
        _, r2 = self._msg("q2", "但是在微信里点续借")
        self.assertEqual(r2.batch.batch_id, r1.batch.batch_id)
        self.assertEqual(r2.batch.message_count, 2)
        self.assertEqual(r2.batch.flush_at_epoch, t0 + 5.0)

        # Unrelated VIEW event and greeting noise do NOT enter batch or push deadline.
        self.clock.advance(0.5)
        self._msg(None, "", msg_type="event", event="VIEW")
        self._msg("inv1", "你好")
        batch_still = self.store.get_batch(r1.batch.batch_id)
        self.assertEqual(batch_still.message_count, 2)
        self.assertEqual(batch_still.flush_at_epoch, t0 + 5.0)

        # Burst at +4.5s -> +7.5s, then +7.0s -> +10.0s, then +8.5s -> still capped at t0 + 10.0s!
        self.clock.advance(2.0)  # +4.5s
        self._msg("q3", "提示不能续借")
        self.clock.advance(2.5)  # +7.0s
        self._msg("q4", "请问是不是有人预约了")
        self.clock.advance(1.5)  # +8.5s
        _, r5 = self._msg("q5", "书名是深度学习")
        self.assertEqual(r5.batch.quiet_deadline_epoch, t0 + 11.5)
        self.assertEqual(r5.batch.max_deadline_epoch, t0 + 10.0)
        self.assertEqual(r5.batch.flush_at_epoch, t0 + 10.0)

        # Before t0 + 10.0s, nothing flushes.
        self.clock.advance(1.4)  # +9.9s
        self.assertEqual(self.store.flush_due_batches(), [])

        # At t0 + 10.0s, batch flushes all 5 in exact receipt order.
        self.clock.advance(0.1)  # +10.0s
        outcomes = self.store.flush_due_batches()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(
            outcomes[0].batch.merged_text,
            "你好，我借的书快到期了\n但是在微信里点续借\n提示不能续借\n请问是不是有人预约了\n书名是深度学习",
        )
        self.assertEqual(outcomes[0].route_stage, "ready_for_triage")

    def test_overdue_open_batch_auto_flushes_before_accepting_new_question(self):
        _, r1 = self._msg("a1", "第一批消息")
        self.clock.advance(3.5)
        _, r2 = self._msg("a2", "第二批消息")
        self.assertEqual(len(r2.pre_flushed), 1)
        self.assertEqual(r2.pre_flushed[0].batch.batch_id, r1.batch.batch_id)
        self.assertEqual(r2.pre_flushed[0].batch.merged_text, "第一批消息")
        self.assertNotEqual(r2.batch.batch_id, r1.batch.batch_id)
        self.assertEqual(r2.batch.message_count, 1)

    def test_same_unresolved_staff_case_appends_until_closed(self):
        case = self.store.create_staff_case(
            account_id="gh_lib",
            reader_id="o_reader_1",
            summary="读者储物柜人脸识别无法打开",
            latest_message="储物柜扫不了我的人脸",
        )
        # Follow-up appends to unresolved case.
        _, f1 = self._msg("s1", "试过了还是扫不了人脸")
        self.clock.advance(3.0)
        out1 = self.store.flush_due_batches()
        self.assertEqual(len(out1), 1)
        self.assertEqual(out1[0].route_stage, "staff_case_appended")
        self.assertEqual(out1[0].staff_case_id, case.case_id)

        cases = self.store.list_staff_cases(status="open")
        self.assertEqual(cases[0]["latest_message"], "试过了还是扫不了人脸")
        self.assertEqual(len(cases[0]["messages"]), 2)

        # Clearly distinct new topic still routes normally.
        _, f2 = self._msg("s2", "请问图书馆几点关门？")
        self.clock.advance(3.0)
        out2 = self.store.flush_due_batches()
        self.assertEqual(out2[0].route_stage, "ready_for_triage")
        self.assertIsNone(out2[0].staff_case_id)

        # Closing the staff case allows subsequent questions to take normal flow.
        closed = self.store.close_staff_case(case.case_id, resolution_note="已在后台联系馆员人工开柜")
        self.assertEqual(closed.status, "closed")
        _, f3 = self._msg("s3", "试过了还是不行")
        self.clock.advance(3.0)
        out3 = self.store.flush_due_batches()
        self.assertEqual(out3[0].route_stage, "ready_for_triage")


class AsyncPipelineAndHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "pipeline.db"
        self.log_path = Path(self.tmpdir.name) / "events.jsonl"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_restart_recovery_flushes_overdue_and_reschedules_pending(self):
        clock = FakeClock()
        store1 = SupportStore(self.db_path, quiet_seconds=3.0, max_wait_seconds=10.0, merge_enabled=True, clock=clock)
        store1.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_past", "msg_type": "text", "msg_id": "p1", "content": "过期批次"},
            classify_message("text", "过期批次"),
        )
        clock.advance(2.5)
        store1.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_future", "msg_type": "text", "msg_id": "p2", "content": "未到期批次"},
            classify_message("text", "未到期批次"),
        )
        store1.close()

        # Simulate downtime of 1.0s (past=3.5s expired; future=1.0s into 3s window -> 2.0s left).
        clock.advance(1.0)
        flushed_texts: list[str] = []

        async def on_flush(outcome):
            flushed_texts.append(outcome.batch.merged_text or "")

        store2 = SupportStore(self.db_path, quiet_seconds=3.0, max_wait_seconds=10.0, merge_enabled=True, clock=clock)
        pipeline = SupportPipeline(store2, on_batch_flushed=on_flush)

        async def scenario():
            recovered = await pipeline.start()
            try:
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].batch.merged_text, "过期批次")
                self.assertEqual(store2.overview_metrics()["open_batches"], 1)
                clock.advance(2.1)
                pipeline.notify_scheduler()
                await asyncio.sleep(0.05)
                self.assertEqual(store2.overview_metrics()["open_batches"], 0)
            finally:
                await pipeline.stop()
                store2.close()

        asyncio.run(scenario())
        self.assertEqual(flushed_texts, ["过期批次", "未到期批次"])

    def test_real_timer_reschedules_and_delivers_emergency_and_feedback(self):
        sent_calls: list[tuple[str, str]] = []
        flushed: list[str] = []

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            sent_calls.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 9, "mode": "direct_https"}

        store = SupportStore(self.db_path, quiet_seconds=0.12, max_wait_seconds=0.35, merge_enabled=True)
        pipeline = SupportPipeline(
            store,
            send_text_fn=fake_send,
            auto_reply_enabled=True,
            on_batch_flushed=lambda out: flushed.append(out.batch.merged_text or ""),
        )

        async def run():
            await pipeline.start()
            try:
                for idx, (reader, content) in enumerate(
                    [
                        ("o_emerg", "我被锁在图书馆了。"),
                        ("o_fb", "建议二楼多加电源插座"),
                        ("o_q", "第一句"),
                    ],
                    start=1,
                ):
                    p = {"to_user": "gh_lib", "from_user": reader, "msg_type": "text", "msg_id": f"r{idx}", "content": content}
                    res = pipeline.ingest_sync(p)
                    await pipeline.process_ingest_side_effects(res, p)

                await asyncio.sleep(0.06)
                # Still before 0.12s quiet window -> extend timer with second fragment.
                p2 = {"to_user": "gh_lib", "from_user": "o_q", "msg_type": "text", "msg_id": "r4", "content": "第二句"}
                res2 = pipeline.ingest_sync(p2)
                await pipeline.process_ingest_side_effects(res2, p2)

                self.assertEqual(flushed, [])
                await asyncio.sleep(0.22)
            finally:
                await pipeline.stop()
                store.close()

        asyncio.run(run())
        self.assertEqual(sent_calls[0], ("o_emerg", EMERGENCY_CONTACT_REPLY))
        self.assertEqual(sent_calls[1], ("o_fb", FEEDBACK_RECEIVED_REPLY))
        self.assertEqual(flushed, ["第一句\n第二句"])

    def test_http_relay_wechat_and_support_endpoints_via_lifespan(self):
        token = "step2-http-token"
        auth = {"authorization": f"Bearer {token}"}

        async def run_http():
            with (
                patch.object(relay_receiver, "_AUTH_TOKEN", token),
                patch.object(relay_receiver, "_LOG_PATH", self.log_path),
                patch.object(relay_receiver, "_SUPPORT_DB_PATH_RAW", str(self.db_path)),
                patch.object(relay_receiver, "_SUPPORT_MERGE_ENABLED", True),
                patch.object(relay_receiver, "_SUPPORT_QUIET_SECONDS", 3.0),
                patch.object(relay_receiver, "_SUPPORT_MAX_WAIT_SECONDS", 10.0),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ENABLED", False),
            ):
                async with relay_receiver._lifespan(relay_receiver.app):
                    self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)

                    # Unauthorized check.
                    unauth_code, _ = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/overview"
                    )
                    self.assertEqual(unauth_code, 401)

                    with self.assertLogs("uvicorn.error", level="INFO") as captured:
                        for idx, body in enumerate(
                            [
                                {"msg_type": "event", "event": "VIEW", "from_user": "o_u1", "to_user": "gh_1"},
                                {"msg_type": "text", "msg_id": "m_fb", "from_user": "o_u1", "to_user": "gh_1", "content": "建议三楼增加饮水机"},
                                {"msg_type": "text", "msg_id": "m_q1", "from_user": "o_u2", "to_user": "gh_1", "content": "预约的书在哪里取"},
                            ],
                            start=1,
                        ):
                            code, text = await _async_asgi_request(
                                relay_receiver.app,
                                "POST",
                                "/relay/wechat",
                                body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                headers=auth,
                            )
                            self.assertEqual(code, 200)
                            self.assertEqual(
                                json.loads(text),
                                {"ok": True, "accepted": True, "duplicate": False, "seq": idx},
                            )

                    logs_joined = "\n".join(captured.output)
                    self.assertNotIn("o_u1", logs_joined)
                    self.assertNotIn("o_u2", logs_joined)
                    self.assertNotIn("预约的书在哪里取", logs_joined)

                    _, ov_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/overview", headers=auth
                    )
                    overview = json.loads(ov_text)
                    self.assertEqual(overview["inbound_categories"]["unsupported"], 1)
                    self.assertEqual(overview["inbound_categories"]["feedback"], 1)
                    self.assertEqual(overview["inbound_categories"]["question"], 1)
                    self.assertEqual(overview["open_batches"], 1)
                    self.assertEqual(overview["outbound_replies"]["shadow"], 1)

                    _, fb_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/feedbacks", headers=auth
                    )
                    feedbacks = json.loads(fb_text)
                    self.assertEqual(feedbacks["count"], 1)
                    self.assertEqual(feedbacks["feedbacks"][0]["content"], "建议三楼增加饮水机")

                    # Create & close staff case through HTTP API (empty JSON close body also supported).
                    _, created_text = await _async_asgi_request(
                        relay_receiver.app,
                        "POST",
                        "/relay/support/cases",
                        body=json.dumps({"account_id": "gh_1", "reader_id": "o_u2", "summary": "借书扣费异常"}).encode("utf-8"),
                        headers=auth,
                    )
                    created = json.loads(created_text)
                    case_id = created["case"]["case_id"]
                    _, closed_text = await _async_asgi_request(
                        relay_receiver.app,
                        "POST",
                        f"/relay/support/cases/{case_id}/close",
                        body=b"",
                        headers=auth,
                    )
                    closed = json.loads(closed_text)
                    self.assertEqual(closed["case"]["status"], "closed")

                    _, rep_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/replies", headers=auth
                    )
                    replies = json.loads(rep_text)
                    self.assertEqual(replies["count"], 1)
                    self.assertEqual(replies["replies"][0]["status"], "shadow")

        asyncio.run(run_http())
        self.assertIsNone(relay_receiver.app.state.support_pipeline)

    def test_schema_v1_to_v2_migration_preserves_existing_rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE feedback_records (
                feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, reader_id TEXT NOT NULL, event_seq INTEGER,
                created_at_epoch REAL NOT NULL, created_at_utc TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'recorded',
                staff_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE staff_cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, reader_id TEXT NOT NULL,
                source_batch_id INTEGER, source_event_seq INTEGER,
                case_kind TEXT NOT NULL DEFAULT 'staff_todo', status TEXT NOT NULL DEFAULT 'open',
                summary TEXT NOT NULL, latest_message TEXT NOT NULL,
                created_at_epoch REAL NOT NULL, created_at_utc TEXT NOT NULL,
                updated_at_epoch REAL NOT NULL, updated_at_utc TEXT NOT NULL,
                closed_at_utc TEXT, resolution_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE merge_batches (
                batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, reader_id TEXT NOT NULL, status TEXT NOT NULL,
                first_received_at_epoch REAL NOT NULL, last_received_at_epoch REAL NOT NULL,
                quiet_deadline_epoch REAL NOT NULL, max_deadline_epoch REAL NOT NULL,
                flush_at_epoch REAL NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
                merged_text TEXT, flushed_at_epoch REAL,
                route_stage TEXT NOT NULL DEFAULT 'pending', staff_case_id INTEGER
            );
            CREATE UNIQUE INDEX idx_merge_open_reader ON merge_batches(account_id, reader_id) WHERE status = 'open';
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '1');
            INSERT INTO merge_batches(
                account_id, reader_id, status, first_received_at_epoch, last_received_at_epoch,
                quiet_deadline_epoch, max_deadline_epoch, flush_at_epoch, message_count
            ) VALUES ('gh_v1', 'o_v1', 'open', 100.0, 100.0, 103.0, 110.0, 103.0, 1);
            """
        )
        conn.commit()
        conn.close()

        store = SupportStore(self.db_path, merge_enabled=False)
        try:
            b = store.get_batch(1)
            self.assertIsNotNone(b)
            self.assertEqual((b.generation, b.preempt_count, b.preempt_locked), (1, 0, False))
            self.assertEqual(store.overview_metrics()["schema_version"], SCHEMA_VERSION)
        finally:
            store.close()

    def test_immediate_mode_preemption_barrier_cap_and_restart_recovery(self):
        clock = FakeClock()
        store = SupportStore(
            self.db_path,
            quiet_seconds=3.0,
            max_wait_seconds=10.0,
            merge_enabled=False,
            max_preempts=2,
            clock=clock,
        )
        r1 = store.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_r1", "msg_type": "text", "msg_id": "i1", "content": "你好请问"},
            classify_message("text", "你好请问"),
        )
        self.assertEqual((r1.batch.status, r1.batch.generation, r1.batch.preempt_count), ("processing", 1, 0))

        # Second message preempts generation 1 before commit.
        clock.advance(0.5)
        r2 = store.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_r1", "msg_type": "text", "msg_id": "i2", "content": "几点关门"},
            classify_message("text", "几点关门"),
        )
        self.assertEqual(r2.preempted_batch_id, r1.batch.batch_id)
        self.assertEqual((r2.batch.generation, r2.batch.preempt_count), (2, 1))

        # Stale generation 1 commit barrier MUST reject.
        stale_commit, _ = store.commit_batch_generation(r1.batch.batch_id, 1, route_stage="ready_for_triage")
        self.assertIsNone(stale_commit)
        self.assertEqual(store.get_batch(r1.batch.batch_id).status, "processing")

        # Duplicate retry of i2 must NOT trigger a 2nd preempt.
        r2_dup = store.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_r1", "msg_type": "text", "msg_id": "i2", "content": "几点关门"},
            classify_message("text", "几点关门"),
        )
        self.assertTrue(r2_dup.duplicate)
        self.assertEqual(store.get_batch(r1.batch.batch_id).preempt_count, 1)

        # Third message hits preempt_count=2, fourth locks batch 1 and queues into batch 2.
        r3 = store.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_r1", "msg_type": "text", "msg_id": "i3", "content": "东海岸校区"},
            classify_message("text", "东海岸校区"),
        )
        self.assertEqual((r3.batch.generation, r3.batch.preempt_count), (3, 2))
        r4 = store.ingest_event(
            {"to_user": "gh_lib", "from_user": "o_r1", "msg_type": "text", "msg_id": "i4", "content": "另外明天开馆吗"},
            classify_message("text", "另外明天开馆吗"),
        )
        self.assertEqual(r4.queued_behind_batch_id, r1.batch.batch_id)
        self.assertEqual(r4.batch.status, "queued")
        self.assertTrue(store.get_batch(r1.batch.batch_id).preempt_locked)

        # Commit batch 1 gen 3 -> atomically promotes queued batch 2 to processing.
        committed1, promoted = store.commit_batch_generation(r1.batch.batch_id, 3, route_stage="ready_for_triage")
        self.assertEqual(committed1.batch.merged_text, "你好请问\n几点关门\n东海岸校区")
        self.assertIsNotNone(promoted)
        self.assertEqual((promoted.batch_id, promoted.status, promoted.merged_text), (r4.batch.batch_id, "processing", "另外明天开馆吗"))
        store.close()

        # Simulate process restart with unfinished batch 2 still in processing status.
        flushed_texts: list[str] = []
        store2 = SupportStore(self.db_path, merge_enabled=False, max_preempts=2, clock=clock)
        pipeline2 = SupportPipeline(store2, on_batch_flushed=lambda out: flushed_texts.append(out.batch.merged_text or ""))

        async def finish_recovery():
            await pipeline2.start()
            try:
                await pipeline2.wait_for_idle(timeout=2.0)
            finally:
                await pipeline2.stop()
                store2.close()

        asyncio.run(finish_recovery())
        self.assertEqual(flushed_texts, ["另外明天开馆吗"])

    def test_immediate_async_preempt_cancels_slow_triage_and_delivers_once(self):
        triage_started: list[str] = []
        cancelled_runs: list[str] = []
        committed_texts: list[str] = []

        async def slow_triage(outcome):
            text = outcome.batch.merged_text or ""
            triage_started.append(text)
            try:
                await asyncio.sleep(0.12)
            except asyncio.CancelledError:
                cancelled_runs.append(text)
                raise

        store = SupportStore(self.db_path, merge_enabled=False, max_preempts=2)
        pipeline = SupportPipeline(
            store,
            triage_fn=slow_triage,
            on_batch_flushed=lambda out: committed_texts.append(out.batch.merged_text or ""),
        )

        async def scenario():
            await pipeline.start()
            try:
                p1 = {"to_user": "gh_lib", "from_user": "o_imm", "msg_type": "text", "msg_id": "imm1", "content": "第一句"}
                res1 = pipeline.ingest_sync(p1)
                await pipeline.process_ingest_side_effects(res1, p1)
                await asyncio.sleep(0.03)

                p2 = {"to_user": "gh_lib", "from_user": "o_imm", "msg_type": "text", "msg_id": "imm2", "content": "第二句"}
                res2 = pipeline.ingest_sync(p2)
                await pipeline.process_ingest_side_effects(res2, p2)

                await pipeline.wait_for_idle(timeout=2.0)
            finally:
                await pipeline.stop()
                store.close()

        asyncio.run(scenario())
        self.assertEqual(triage_started, ["第一句", "第一句\n第二句"])
        self.assertEqual(cancelled_runs, ["第一句"])
        self.assertEqual(committed_texts, ["第一句\n第二句"])

    def test_http_relay_wechat_with_merge_disabled_switch(self):
        token = "immediate-http-token"
        auth = {"authorization": f"Bearer {token}"}

        async def run_http():
            with (
                patch.object(relay_receiver, "_AUTH_TOKEN", token),
                patch.object(relay_receiver, "_LOG_PATH", self.log_path),
                patch.object(relay_receiver, "_SUPPORT_DB_PATH_RAW", str(self.db_path)),
                patch.object(relay_receiver, "_SUPPORT_MERGE_ENABLED", False),
                patch.object(relay_receiver, "_SUPPORT_IMMEDIATE_MAX_PREEMPTS", 2),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ENABLED", False),
            ):
                async with relay_receiver._lifespan(relay_receiver.app):
                    p = {"msg_type": "text", "msg_id": "hi1", "from_user": "o_fast", "to_user": "gh_1", "content": "怎么借书"}
                    code, _ = await _async_asgi_request(
                        relay_receiver.app,
                        "POST",
                        "/relay/wechat",
                        body=json.dumps(p, ensure_ascii=False).encode("utf-8"),
                        headers=auth,
                    )
                    self.assertEqual(code, 200)
                    await relay_receiver.app.state.support_pipeline.wait_for_idle(timeout=2.0)

                    _, ov_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/overview", headers=auth
                    )
                    overview = json.loads(ov_text)
                    self.assertFalse(overview["merge_enabled"])
                    self.assertEqual(overview["max_preempts"], 2)
                    self.assertEqual(overview["open_batches"], 0)
                    self.assertEqual(overview["flushed_batches"], 1)

        asyncio.run(run_http())

    def test_auto_reply_allowlist_sends_only_to_allowed_readers_and_shadows_others(self):
        sent_calls: list[tuple[str, str]] = []

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            sent_calls.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 8, "mode": "direct_https"}

        store = SupportStore(self.db_path, merge_enabled=False)
        pipeline = SupportPipeline(
            store,
            send_text_fn=fake_send,
            auto_reply_enabled=True,
            auto_reply_allowlist=["o_allowed_tester"],
        )

        async def scenario():
            await pipeline.start()
            try:
                for mid, reader, content in [
                    ("a1", "o_allowed_tester", "建议三楼增加电源"),
                    ("a2", "o_regular_reader", "建议四楼增加座位"),
                ]:
                    payload = {
                        "to_user": "gh_lib",
                        "from_user": reader,
                        "msg_type": "text",
                        "msg_id": mid,
                        "content": content,
                    }
                    res = pipeline.ingest_sync(payload)
                    await pipeline.process_ingest_side_effects(res, payload)
                await pipeline.wait_for_idle(timeout=2.0)
            finally:
                await pipeline.stop()

        asyncio.run(scenario())
        self.assertEqual(len(sent_calls), 1)
        self.assertEqual(sent_calls[0][0], "o_allowed_tester")
        replies = store.list_outbound_replies(limit=10)
        statuses_by_reader = {r["reader_id"]: r["status"] for r in replies}
        self.assertEqual(statuses_by_reader["o_allowed_tester"], "sent")
        self.assertEqual(statuses_by_reader["o_regular_reader"], "shadow")
        store.close()

    def test_http_support_auto_reply_allowlist_via_lifespan(self):
        token = "allowlist-http-token"
        auth = {"authorization": f"Bearer {token}"}
        sent_calls: list[tuple[str, str]] = []

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            sent_calls.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 11, "mode": "direct_https"}

        async def run_http():
            parsed = relay_receiver._parse_allowlist("o_tester_vip, o_tester_2")
            self.assertEqual(parsed, frozenset({"o_tester_vip", "o_tester_2"}))
            with (
                patch.object(relay_receiver, "_AUTH_TOKEN", token),
                patch.object(relay_receiver, "_LOG_PATH", self.log_path),
                patch.object(relay_receiver, "_SUPPORT_DB_PATH_RAW", str(self.db_path)),
                patch.object(relay_receiver, "_SUPPORT_MERGE_ENABLED", False),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ENABLED", True),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ALLOWLIST", parsed),
                patch.object(relay_receiver._DIRECT_CLIENT, "send_text", side_effect=fake_send),
            ):
                async with relay_receiver._lifespan(relay_receiver.app):
                    with self.assertLogs("uvicorn.error", level="INFO") as captured:
                        for mid, reader, text in [
                            ("vip1", "o_tester_vip", "开馆时间？"),
                            ("reg1", "o_public_user", "开馆时间？"),
                        ]:
                            await _asgi_req_payload(mid, reader, text)
                        await relay_receiver.app.state.support_pipeline.wait_for_idle(timeout=2.0)

                    logs_joined = "\n".join(captured.output)
                    self.assertNotIn("o_tester_vip", logs_joined)
                    self.assertNotIn("o_public_user", logs_joined)

                    _, ov_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/overview", headers=auth
                    )
                    self.assertNotIn("o_tester_vip", ov_text)
                    overview = json.loads(ov_text)
                    self.assertTrue(overview["auto_reply_enabled"])
                    self.assertEqual(overview["auto_reply_allowlist_count"], 2)
                    self.assertEqual(overview["outbound_replies"], {"sent": 1, "shadow": 1})

        async def _asgi_req_payload(mid: str, reader: str, text: str):
            body = json.dumps(
                {"msg_type": "text", "msg_id": mid, "from_user": reader, "to_user": "gh_lib", "content": text},
                ensure_ascii=False,
            ).encode("utf-8")
            code, _ = await _async_asgi_request(relay_receiver.app, "POST", "/relay/wechat", body=body, headers=auth)
            self.assertEqual(code, 200)

        asyncio.run(run_http())
        self.assertEqual(len(sent_calls), 1)
        self.assertEqual(sent_calls[0][0], "o_tester_vip")

    def test_gray_enroll_keyword_persists_allowlist_and_unlocks_live_replies(self):
        sent_calls: list[tuple[str, str]] = []

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            sent_calls.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 6, "mode": "direct_https"}

        test_kw = "灰度测试暗号"
        catalog = PresetCatalog.from_corpus_dir(Path("rag-poc/corpus"))
        store1 = SupportStore(self.db_path, merge_enabled=False)
        try:
            responder1 = SupportResponder(store=store1, preset_catalog=catalog)
            pipeline1 = SupportPipeline(
                store1,
                send_text_fn=fake_send,
                auto_reply_enabled=True,
                auto_reply_allowlist=["o_static_admin"],
                gray_enroll_keyword=test_kw,
                triage_fn=responder1.triage,
            )
            pipeline1.commit_handler = lambda outcome, plan: responder1.apply_committed_plan(
                outcome, plan, deliver_reply_fn=pipeline1.deliver_reply
            )

            async def run_phase1():
                await pipeline1.start()
                try:
                    # Before enrolling, o_guest stays in shadow mode.
                    p_before = {
                        "to_user": "gh_lib",
                        "from_user": "o_guest",
                        "msg_type": "text",
                        "msg_id": "g1",
                        "content": "开馆时间？",
                    }
                    res_before = pipeline1.ingest_sync(p_before)
                    await pipeline1.process_ingest_side_effects(res_before, p_before)
                    await pipeline1.wait_for_idle(timeout=2.0)

                    store1.create_staff_case(
                        account_id="gh_lib",
                        reader_id="o_guest",
                        summary="读者储物柜故障",
                        latest_message="储物柜打不开",
                    )
                    store1.set_active_clarification(
                        account_id="gh_lib",
                        reader_id="o_guest",
                        original_batch_id=None,
                        original_question="预约",
                        clarify_prompt="请问预约什么？",
                    )

                    for mid, reader, content in [
                        ("g2", "o_guest", f"「{test_kw}」"),
                        ("g2", "o_guest", f"「{test_kw}」"),  # duplicate MsgId retry ignored
                        ("g3", "o_guest", f"{test_kw}！"),
                        ("g4", "o_static_admin", test_kw),
                    ]:
                        payload = {
                            "to_user": "gh_lib",
                            "from_user": reader,
                            "msg_type": "text",
                            "msg_id": mid,
                            "content": content,
                        }
                        res = pipeline1.ingest_sync(payload)
                        await pipeline1.process_ingest_side_effects(res, payload)
                    await pipeline1.wait_for_idle(timeout=2.0)

                    # Backdoor command must neither append to open staff case nor consume active clarification.
                    open_cases = store1.list_staff_cases(status="open")
                    self.assertEqual(len(open_cases[0]["messages"]), 1)
                    self.assertIsNotNone(store1.get_active_clarification("gh_lib", "o_guest"))
                    store1.close_staff_case(open_cases[0]["case_id"], resolution_note="测试结案")

                    for mid, reader, content in [
                        ("g5", "o_guest", "开馆时间？"),
                        ("g6", "o_bystander", "开馆时间？"),
                    ]:
                        payload = {
                            "to_user": "gh_lib",
                            "from_user": reader,
                            "msg_type": "text",
                            "msg_id": mid,
                            "content": content,
                        }
                        res = pipeline1.ingest_sync(payload)
                        await pipeline1.process_ingest_side_effects(res, payload)
                    await pipeline1.wait_for_idle(timeout=2.0)
                finally:
                    await pipeline1.stop()

            asyncio.run(run_phase1())
            self.assertEqual(
                sent_calls,
                [
                    ("o_guest", GRAY_ENROLL_ADDED_REPLY),
                    ("o_guest", GRAY_ENROLL_ALREADY_REPLY),
                    ("o_static_admin", GRAY_ENROLL_ALREADY_REPLY),
                    ("o_guest", catalog.get_by_id("preset_opening_hours").body),
                ],
            )
        finally:
            store1.close()

        # Re-open SQLite across restart and verify dynamic allowlist survives.
        sent_after_restart: list[tuple[str, str]] = []

        def fake_send_2(to_user: str, content: str) -> dict[str, object]:
            sent_after_restart.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 5, "mode": "direct_https"}

        store2 = SupportStore(self.db_path, merge_enabled=False)
        try:
            responder2 = SupportResponder(store=store2, preset_catalog=catalog)
            pipeline2 = SupportPipeline(
                store2,
                send_text_fn=fake_send_2,
                auto_reply_enabled=True,
                auto_reply_allowlist=["o_static_admin"],
                triage_fn=responder2.triage,
            )
            pipeline2.commit_handler = lambda outcome, plan: responder2.apply_committed_plan(
                outcome, plan, deliver_reply_fn=pipeline2.deliver_reply
            )

            async def run_phase2():
                await pipeline2.start()
                try:
                    p = {
                        "to_user": "gh_lib",
                        "from_user": "o_guest",
                        "msg_type": "text",
                        "msg_id": "g7",
                        "content": "图书续借？",
                    }
                    res = pipeline2.ingest_sync(p)
                    await pipeline2.process_ingest_side_effects(res, p)
                    await pipeline2.wait_for_idle(timeout=2.0)
                finally:
                    await pipeline2.stop()

            asyncio.run(run_phase2())
            self.assertEqual(
                sent_after_restart,
                [("o_guest", catalog.get_by_id("preset_book_renewal").body)],
            )
        finally:
            store2.close()

    def test_http_gray_enroll_overview_list_and_remove(self):
        token = "gray-enroll-http-token"
        auth = {"authorization": f"Bearer {token}"}
        sent_calls: list[tuple[str, str]] = []
        test_kw = "test-gray-passphrase"

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            sent_calls.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 7, "mode": "direct_https"}

        async def run_default_disabled():
            with (
                patch.object(relay_receiver, "_AUTH_TOKEN", token),
                patch.object(relay_receiver, "_LOG_PATH", self.log_path),
                patch.object(relay_receiver, "_SUPPORT_DB_PATH_RAW", str(self.db_path)),
                patch.object(relay_receiver, "_SUPPORT_CORPUS_DIR_RAW", "rag-poc/corpus"),
                patch.object(relay_receiver, "_SUPPORT_INDEX_DIR_RAW", str(Path(self.tmpdir.name) / "no_index")),
                patch.object(relay_receiver, "_SUPPORT_DECISION_BASE_URL", ""),
                patch.object(relay_receiver, "_SUPPORT_MERGE_ENABLED", False),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ENABLED", True),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ALLOWLIST", frozenset({"o_admin"})),
            ):
                async with relay_receiver._lifespan(relay_receiver.app):
                    self.assertEqual(relay_receiver._SUPPORT_GRAY_ENROLL_KEYWORD, "")
                    _, ov_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/overview", headers=auth
                    )
                    ov = json.loads(ov_text)
                    self.assertFalse(ov["gray_enroll_enabled"])
                    self.assertEqual(ov["dynamic_allowlist_count"], 0)

        asyncio.run(run_default_disabled())

        async def run_http():
            with (
                patch.object(relay_receiver, "_AUTH_TOKEN", token),
                patch.object(relay_receiver, "_LOG_PATH", self.log_path),
                patch.object(relay_receiver, "_SUPPORT_DB_PATH_RAW", str(self.db_path)),
                patch.object(relay_receiver, "_SUPPORT_CORPUS_DIR_RAW", "rag-poc/corpus"),
                patch.object(relay_receiver, "_SUPPORT_INDEX_DIR_RAW", str(Path(self.tmpdir.name) / "no_index")),
                patch.object(relay_receiver, "_SUPPORT_DECISION_BASE_URL", ""),
                patch.object(relay_receiver, "_SUPPORT_MERGE_ENABLED", False),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ENABLED", True),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ALLOWLIST", frozenset({"o_admin"})),
                patch.object(relay_receiver, "_SUPPORT_GRAY_ENROLL_KEYWORD", test_kw),
                patch.object(relay_receiver._DIRECT_CLIENT, "send_text", side_effect=fake_send),
            ):
                async with relay_receiver._lifespan(relay_receiver.app):
                    with self.assertLogs("uvicorn.error", level="INFO") as captured:
                        for mid, reader, content in [
                            ("h1", "o_tester_new", test_kw),
                            ("h2", "o_tester_new", "开馆时间？"),
                        ]:
                            code, _ = await _async_asgi_request(
                                relay_receiver.app,
                                "POST",
                                "/relay/wechat",
                                body=json.dumps(
                                    {
                                        "msg_type": "text",
                                        "msg_id": mid,
                                        "from_user": reader,
                                        "to_user": "gh_lib",
                                        "content": content,
                                    },
                                    ensure_ascii=False,
                                ).encode("utf-8"),
                                headers=auth,
                            )
                            self.assertEqual(code, 200)
                        await relay_receiver.app.state.support_pipeline.wait_for_idle(timeout=2.0)

                    logs_joined = "\n".join(captured.output)
                    self.assertNotIn("o_tester_new", logs_joined)
                    self.assertNotIn(test_kw, logs_joined)

                    _, ov_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/overview", headers=auth
                    )
                    self.assertNotIn("o_tester_new", ov_text)
                    self.assertNotIn(test_kw, ov_text)
                    ov = json.loads(ov_text)
                    self.assertEqual(ov["auto_reply_allowlist_count"], 2)
                    self.assertEqual(ov["dynamic_allowlist_count"], 1)
                    self.assertTrue(ov["gray_enroll_enabled"])
                    self.assertEqual(ov["inbound_categories"]["gray_enroll"], 1)

                    _, al_text = await _async_asgi_request(
                        relay_receiver.app, "GET", "/relay/support/allowlist", headers=auth
                    )
                    al = json.loads(al_text)
                    self.assertEqual(al["static_count"], 1)
                    self.assertEqual(al["count"], 1)
                    self.assertEqual(al["entries"][0]["reader_id"], "o_tester_new")

                    rm_code, rm_text = await _async_asgi_request(
                        relay_receiver.app,
                        "POST",
                        "/relay/support/allowlist/remove",
                        body=json.dumps({"reader_id": "o_tester_new"}).encode("utf-8"),
                        headers=auth,
                    )
                    self.assertEqual(rm_code, 200)
                    self.assertEqual(json.loads(rm_text), {"removed": 1})

                    # Once removed, subsequent messages go back to shadow mode.
                    code3, _ = await _async_asgi_request(
                        relay_receiver.app,
                        "POST",
                        "/relay/wechat",
                        body=json.dumps(
                            {
                                "msg_type": "text",
                                "msg_id": "h3",
                                "from_user": "o_tester_new",
                                "to_user": "gh_lib",
                                "content": "开馆时间？",
                            },
                            ensure_ascii=False,
                        ).encode("utf-8"),
                        headers=auth,
                    )
                    self.assertEqual(code3, 200)
                    await relay_receiver.app.state.support_pipeline.wait_for_idle(timeout=2.0)

                    # Re-enroll and verify clear removes all entries.
                    await _async_asgi_request(
                        relay_receiver.app,
                        "POST",
                        "/relay/wechat",
                        body=json.dumps(
                            {
                                "msg_type": "text",
                                "msg_id": "h4",
                                "from_user": "o_tester_new",
                                "to_user": "gh_lib",
                                "content": test_kw,
                            },
                            ensure_ascii=False,
                        ).encode("utf-8"),
                        headers=auth,
                    )
                    await relay_receiver.app.state.support_pipeline.wait_for_idle(timeout=2.0)
                    clr_code, clr_text = await _async_asgi_request(
                        relay_receiver.app, "POST", "/relay/support/allowlist/clear", body=b"", headers=auth
                    )
                    self.assertEqual(clr_code, 200)
                    self.assertEqual(json.loads(clr_text), {"removed": 1})

        asyncio.run(run_http())
        self.assertEqual(len(sent_calls), 3)
        self.assertEqual(sent_calls[0][1], GRAY_ENROLL_ADDED_REPLY)

    def test_empty_static_allowlist_still_gates_when_gray_enroll_keyword_set(self):
        sent_calls: list[tuple[str, str]] = []
        test_kw = "test-empty-static-passphrase"

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            sent_calls.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 5, "mode": "direct_https"}

        catalog = PresetCatalog.from_corpus_dir(Path("rag-poc/corpus"))
        store = SupportStore(self.db_path, merge_enabled=False)
        try:
            responder = SupportResponder(store=store, preset_catalog=catalog)
            pipeline = SupportPipeline(
                store,
                send_text_fn=fake_send,
                auto_reply_enabled=True,
                auto_reply_allowlist=[],
                gray_enroll_keyword=test_kw,
                triage_fn=responder.triage,
            )
            pipeline.commit_handler = lambda outcome, plan: responder.apply_committed_plan(
                outcome, plan, deliver_reply_fn=pipeline.deliver_reply
            )

            async def run():
                await pipeline.start()
                try:
                    for mid, reader, content in [
                        ("e1", "o_user_1", "开馆时间？"),
                        ("e2", "o_stranger", "开馆时间？"),
                    ]:
                        p = {"to_user": "gh_lib", "from_user": reader, "msg_type": "text", "msg_id": mid, "content": content}
                        res = pipeline.ingest_sync(p)
                        await pipeline.process_ingest_side_effects(res, p)
                    await pipeline.wait_for_idle(timeout=2.0)
                    self.assertEqual(sent_calls, [])

                    for mid, reader, content in [
                        ("e3", "o_user_1", test_kw),
                        ("e4", "o_user_1", "开馆时间？"),
                        ("e5", "o_stranger", "开馆时间？"),
                    ]:
                        p = {"to_user": "gh_lib", "from_user": reader, "msg_type": "text", "msg_id": mid, "content": content}
                        res = pipeline.ingest_sync(p)
                        await pipeline.process_ingest_side_effects(res, p)
                    await pipeline.wait_for_idle(timeout=2.0)
                finally:
                    await pipeline.stop()

            asyncio.run(run())
            self.assertEqual(
                sent_calls,
                [
                    ("o_user_1", GRAY_ENROLL_ADDED_REPLY),
                    ("o_user_1", catalog.get_by_id("preset_opening_hours").body),
                ],
            )
            replies = store.list_outbound_replies(limit=10)
            shadow_readers = [r["reader_id"] for r in replies if r["status"] == "shadow"]
            self.assertEqual(shadow_readers.count("o_stranger"), 2)
            self.assertEqual(shadow_readers.count("o_user_1"), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
