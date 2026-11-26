"""Tests for Step 3: presets, `/v1/systemone` client, `0.` chunking, clarification, and fallback."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("WECHAT_TOKEN", "test-only-token")

import relay_receiver
from support_backend import (
    DEFAULT_CLARIFY_QUESTION,
    FALLBACK_TEMPLATE_REPLY,
    PresetCatalog,
    PresetDocument,
    SupportPipeline,
    SupportResponder,
    SupportStore,
    SystemOneClient,
    Tier2Result,
    build_chunks_from_corpus,
    build_systemone_url,
    format_grounded_answer,
    normalize_alias_text,
)


async def _asgi_req(
    application,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
        "state": {},
    }
    sent_done = False
    code = 500
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent_done
        if sent_done:
            return {"type": "http.disconnect"}
        sent_done = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        nonlocal code
        if msg["type"] == "http.response.start":
            code = msg["status"]
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    await application(scope, receive, send)
    return code, b"".join(chunks).decode("utf-8")


class FakeTier2Responder:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict[str, object]] = []

    def query(self, question: str, *, clarification_context: str = "", allow_clarify: bool = True) -> Tier2Result:
        self.calls.append(
            {
                "question": question,
                "clarification_context": clarification_context,
                "allow_clarify": allow_clarify,
            }
        )
        return self.handler(question, clarification_context, allow_clarify)


class PresetsAndKnowledgeTests(unittest.TestCase):
    def test_exact_alias_normalization_and_collision_guard(self):
        self.assertEqual(normalize_alias_text("　图书续借？？！～～... "), "图书续借")
        catalog = PresetCatalog.from_corpus_dir(Path("rag-poc/corpus"))
        self.assertGreaterEqual(len(catalog.presets), 3)

        self.assertEqual(catalog.match_exact("图书续借？！").preset_id, "preset_book_renewal")
        self.assertEqual(catalog.match_exact("  图书馆几点关门？ ").preset_id, "preset_opening_hours")
        self.assertEqual(catalog.match_exact("我要预约图书～").preset_id, "preset_book_reservation")

        # Troubleshooting or ambiguous queries MUST NOT falsely match exact aliases.
        self.assertIsNone(catalog.match_exact("图书续借出问题了"))
        self.assertIsNone(catalog.match_exact("预约"))
        self.assertIsNone(catalog.match_exact("静音舱怎么预约"))
        self.assertIsNone(catalog.match_exact("校外读者证续借"))

        with self.assertRaises(ValueError):
            PresetCatalog(
                [
                    PresetDocument("p1", "A", ("续借",), "", "body1", Path("1.a.md")),
                    PresetDocument("p2", "B", ("续借？",), "", "body2", Path("1.b.md")),
                ]
            )

    def test_zero_only_corpus_chunking_strips_yaml_and_preserves_citations(self):
        chunks = build_chunks_from_corpus(Path("rag-poc/corpus"))
        self.assertGreater(len(chunks), 20)
        file_names = {c.file_name for c in chunks}
        self.assertTrue(all(name.startswith("0.") for name in file_names))
        self.assertNotIn("1. 图书续借.md", file_names)
        self.assertNotIn("9. 本馆简介.md", file_names)

        for c in chunks:
            self.assertTrue(c.source_url.startswith("https://"))
            self.assertTrue(c.header_path.startswith("/"))
            self.assertTrue(c.section_title)
            self.assertNotIn("tags:\n", c.text)

    def test_long_and_short_grounded_answer_formatting(self):
        short_out, is_long_1 = format_grounded_answer(
            answer="日常每天 8:00 至 22:00 开放。",
            short_conclusion="日常 8:00–22:00 开放。",
            source_url="https://www.lib.stu.edu.cn/article-11.html",
            section_title="图书借还",
            long_threshold=100,
        )
        self.assertFalse(is_long_1)
        self.assertEqual(short_out, "日常每天 8:00 至 22:00 开放。")

        long_out, is_long_2 = format_grounded_answer(
            answer="这是一段很长的详细说明。" * 30 + " 还带了模型瞎编链接 https://fake.example/123",
            short_conclusion="预约图书归还后保留3天，请登录系统操作（详见 https://fake.example/123）。",
            source_url="https://www.lib.stu.edu.cn/article-11.html",
            section_title="预约图书",
            long_threshold=80,
        )
        self.assertTrue(is_long_2)
        self.assertNotIn("fake.example", long_out)
        self.assertIn("具体请参考 https://www.lib.stu.edu.cn/article-11.html 下的「预约图书」节。", long_out)


class SystemOneClientLoopbackTests(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(build_systemone_url("https://api.example.com"), "https://api.example.com/v1/systemone")
        self.assertEqual(build_systemone_url("https://api.example.com/"), "https://api.example.com/v1/systemone")
        self.assertEqual(build_systemone_url("https://api.example.com/v1"), "https://api.example.com/v1/systemone")
        self.assertEqual(build_systemone_url("https://api.example.com/v1/systemone"), "https://api.example.com/v1/systemone")
        with self.assertRaises(ValueError):
            build_systemone_url("https://user:pass@api.example.com")

    def test_systemone_http_loopback_and_timeout(self):
        received: list[tuple[str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                received.append((self.path, body))
                if body.get("state") == "slow":
                    time.sleep(0.35)
                resp = {
                    "answers": {
                        "route": {"choice": "public_qa", "confidence": 0.92, "probabilities": {"public_qa": 0.92}},
                        "preset_id": {"choice": "none", "confidence": 0.88, "probabilities": {"none": 0.88}},
                    }
                }
                encoded = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = SystemOneClient(
                base_url=f"http://127.0.0.1:{port}",
                api_key="test-key",
                model="decision-model-preview",
                timeout_seconds=0.15,
            )
            answers, duration_ms = client.evaluate(
                state="图书续借失败怎么办",
                questions={"route": {"type": "choice", "instructions": "r", "criteria": {"public_qa": "qa"}}, "preset_id": {"type": "choice", "instructions": "p", "criteria": {"none": "n"}}},
            )
            self.assertEqual(received[0][0], "/v1/systemone")
            self.assertEqual(received[0][1]["model"], "decision-model-preview")
            self.assertEqual(answers["route"].choice, "public_qa")
            self.assertAlmostEqual(answers["route"].confidence or 0.0, 0.92)
            self.assertGreaterEqual(duration_ms, 0)

            with self.assertRaises(Exception) as ctx:
                client.evaluate(state="slow", questions={"route": {"type": "choice", "instructions": "r", "criteria": {"public_qa": "qa"}}})
            self.assertEqual(ctx.exception.reason_code, "timeout")
        finally:
            server.shutdown()
            server.server_close()


class Step3OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "step3.db"
        self.log_path = Path(self.tmpdir.name) / "relay.jsonl"
        self.catalog = PresetCatalog.from_corpus_dir(Path("rag-poc/corpus"))

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_question(self, pipeline: SupportPipeline, msg_id: str, reader: str, text: str) -> None:
        payload = {
            "to_user": "gh_lib",
            "from_user": reader,
            "msg_type": "text",
            "msg_id": msg_id,
            "content": text,
        }
        res = pipeline.ingest_sync(payload)
        return res, payload

    def test_decision_timeout_degrades_to_tier2_and_delivery_failure_keeps_staff_todo(self):
        sent: list[tuple[str, str]] = []

        def flaky_send(to_user: str, content: str) -> dict[str, object]:
            sent.append((to_user, content))
            if "登记至馆员待办队列" in content:
                raise RuntimeError("wechat timeout")
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 5, "mode": "direct_https"}

        class TimeoutDecisionClient:
            def evaluate(self, *, state, questions):
                from support_backend import DecisionClientError
                raise DecisionClientError("timeout")

        def tier2_handler(q: str, ctx: str, allow_clarify: bool) -> Tier2Result:
            if "续借出问题" in q:
                return Tier2Result(
                    outcome="answered",
                    reply_text="请先检查图书是否已逾期或被他人预约，若仍失败可联系总服务台 0754-8650 2958。",
                    short_conclusion="检查是否逾期或被预约。",
                    source_url="https://www.lib.stu.edu.cn/faq-page",
                    section_title="书刊借阅",
                )
            return Tier2Result(outcome="unsupported")

        tier2 = FakeTier2Responder(tier2_handler)
        store = SupportStore(self.db_path, merge_enabled=False)
        responder = SupportResponder(
            store=store,
            preset_catalog=self.catalog,
            decision_client=TimeoutDecisionClient(),
            knowledge_responder=tier2,
        )
        pipeline = SupportPipeline(
            store,
            send_text_fn=flaky_send,
            auto_reply_enabled=True,
            triage_fn=responder.triage,
        )
        pipeline.commit_handler = lambda outcome, plan: responder.apply_committed_plan(
            outcome, plan, deliver_reply_fn=pipeline.deliver_reply
        )

        async def scenario():
            await pipeline.start()
            try:
                for mid, reader, text in [
                    ("d1", "o_exact", "图书续借？"),
                    ("d2", "o_timeout_ok", "图书续借出问题了怎么排查"),
                    ("d3", "o_fallback_fail", "怎么查询我在学校图书馆的个人欠款详情"),
                ]:
                    res, payload = self._run_question(pipeline, mid, reader, text)
                    await pipeline.process_ingest_side_effects(res, payload)
                await pipeline.wait_for_idle(timeout=2.0)
            finally:
                await pipeline.stop()
                store.close()

        asyncio.run(scenario())

        # Re-open store to verify persisted audit & staff to-do despite send exception.
        verify_store = SupportStore(self.db_path, merge_enabled=False)
        try:
            runs_by_batch = {r["batch_id"]: r for r in verify_store.list_triage_runs(limit=10)}
            self.assertEqual(
                [runs_by_batch[1]["stage_path"], runs_by_batch[2]["stage_path"], runs_by_batch[3]["stage_path"]],
                ["exact_alias", "tier2_short", "tier2_fallback"],
            )
            self.assertEqual(runs_by_batch[2]["decision_route"], "timeout")
            cases = verify_store.list_staff_cases(status="open")
            self.assertEqual(len(cases), 1)
            self.assertIn("个人欠款详情", cases[0]["summary"])
            replies = verify_store.list_outbound_replies(limit=10)
            failed_replies = [r for r in replies if r["status"] == "failed"]
            self.assertEqual(len(failed_replies), 1)
            self.assertIn("o_exact", {user for user, _ in sent})
        finally:
            verify_store.close()

    def test_single_clarification_limit_and_preemption_do_not_consume_early(self):
        class ClarifyDecisionClient:
            def __init__(self) -> None:
                self.calls = 0

            def evaluate(self, *, state, questions):
                from support_backend import DecisionAnswer
                self.calls += 1
                q_text = state["question"]
                if "静音舱" in q_text and "东海岸" in q_text:
                    return ({"route": DecisionAnswer(choice="public_qa"), "preset_id": DecisionAnswer(choice="none", confidence=0.1)}, 10)
                return ({"route": DecisionAnswer(choice="clarify", confidence=0.9), "preset_id": DecisionAnswer(choice="none", confidence=0.1)}, 12)

        async def slow_tier2_handler_wrap(outcome):
            return await responder.triage(outcome)

        class SlowTier2:
            def query(self, question: str, *, clarification_context: str = "", allow_clarify: bool = True):
                if "静音舱" in question and "东海岸" in question:
                    return Tier2Result(
                        outcome="answered",
                        reply_text="东海岸静音舱请通过图书馆空间预约系统办理。",
                        source_url="https://www.lib.stu.edu.cn/article-11.html",
                        section_title="空间预约",
                    )
                return Tier2Result(outcome="need_clarify", clarify_question="请问需要哪方面的详情？")

        decision = ClarifyDecisionClient()
        store = SupportStore(self.db_path, merge_enabled=False, max_preempts=2)
        responder = SupportResponder(
            store=store,
            preset_catalog=self.catalog,
            decision_client=decision,
            knowledge_responder=SlowTier2(),
            clarify_generator=lambda q, sys_p: "请问您想预约图书，还是预约静音舱/研讨室等空间？",
        )
        pipeline = SupportPipeline(store, auto_reply_enabled=False, triage_fn=slow_tier2_handler_wrap)
        pipeline.commit_handler = lambda outcome, plan: responder.apply_committed_plan(
            outcome, plan, deliver_reply_fn=pipeline.deliver_reply
        )

        async def run():
            await pipeline.start()
            try:
                # User 1: asks ambiguous "预约" -> single clarification prompt stored.
                res1, p1 = self._run_question(pipeline, "c1", "o_c1", "预约")
                await pipeline.process_ingest_side_effects(res1, p1)
                await pipeline.wait_for_idle(timeout=2.0)
                self.assertEqual(store.overview_metrics()["active_clarifications"], 1)

                # User 1 replies with two quick split bubbles ("静音舱" then "东海岸"), exercising preemption.
                res2, p2 = self._run_question(pipeline, "c2", "o_c1", "静音舱")
                await pipeline.process_ingest_side_effects(res2, p2)
                res3, p3 = self._run_question(pipeline, "c3", "o_c1", "东海岸")
                await pipeline.process_ingest_side_effects(res3, p3)
                await pipeline.wait_for_idle(timeout=2.0)

                self.assertEqual(store.overview_metrics()["active_clarifications"], 0)

                # User 2: hits clarify ONCE, replies ambiguously again -> hits 1-clarification limit -> Fallback + staff_todo!
                res4, p4 = self._run_question(pipeline, "c4", "o_c2", "请问一下那个")
                await pipeline.process_ingest_side_effects(res4, p4)
                await pipeline.wait_for_idle(timeout=2.0)

                res5, p5 = self._run_question(pipeline, "c5", "o_c2", "就是那个")
                await pipeline.process_ingest_side_effects(res5, p5)
                await pipeline.wait_for_idle(timeout=2.0)
            finally:
                await pipeline.stop()

        asyncio.run(run())
        try:
            runs = list(reversed(store.list_triage_runs(limit=10)))
            self.assertEqual(runs[0]["stage_path"], "decision_clarify")
            self.assertEqual(runs[1]["stage_path"], "tier2_short")
            self.assertIn("东海岸", runs[1]["reply_preview"])
            self.assertEqual(runs[2]["stage_path"], "decision_clarify")
            self.assertEqual(runs[3]["stage_path"], "clarify_limit_fallback")
            self.assertEqual(store.overview_metrics()["open_staff_cases"], 1)
        finally:
            store.close()

    def test_http_endpoints_triage_runs_and_presets(self):
        token = "step3-http-tok"
        auth = {"authorization": f"Bearer {token}"}

        async def run_http():
            with (
                patch.object(relay_receiver, "_AUTH_TOKEN", token),
                patch.object(relay_receiver, "_LOG_PATH", self.log_path),
                patch.object(relay_receiver, "_SUPPORT_DB_PATH_RAW", str(self.db_path)),
                patch.object(relay_receiver, "_SUPPORT_CORPUS_DIR_RAW", "rag-poc/corpus"),
                patch.object(relay_receiver, "_SUPPORT_INDEX_DIR_RAW", str(Path(self.tmpdir.name) / "no_index")),
                patch.object(relay_receiver, "_SUPPORT_DECISION_BASE_URL", ""),
                patch.object(relay_receiver, "_SUPPORT_MERGE_ENABLED", False),
                patch.object(relay_receiver, "_SUPPORT_AUTO_REPLY_ENABLED", False),
            ):
                async with relay_receiver._lifespan(relay_receiver.app):
                    _, p_text = await _asgi_req(relay_receiver.app, "GET", "/relay/support/presets", headers=auth)
                    presets_body = json.loads(p_text)
                    self.assertGreaterEqual(presets_body["count"], 3)

                    code, _ = await _asgi_req(
                        relay_receiver.app,
                        "POST",
                        "/relay/wechat",
                        body=json.dumps(
                            {
                                "msg_type": "text",
                                "msg_id": "step3_exact_1",
                                "from_user": "o_http",
                                "to_user": "gh_lib",
                                "content": "开馆时间？",
                            },
                            ensure_ascii=False,
                        ).encode("utf-8"),
                        headers=auth,
                    )
                    self.assertEqual(code, 200)
                    await relay_receiver.app.state.support_pipeline.wait_for_idle(timeout=2.0)

                    _, runs_text = await _asgi_req(
                        relay_receiver.app, "GET", "/relay/support/triage-runs", headers=auth
                    )
                    runs = json.loads(runs_text)
                    self.assertEqual(runs["count"], 1)
                    self.assertEqual(runs["runs"][0]["stage_path"], "exact_alias")
                    self.assertEqual(runs["runs"][0]["preset_id"], "preset_opening_hours")

        asyncio.run(run_http())


if __name__ == "__main__":
    unittest.main()
