"""Consumer-facing tests for the plaintext callback and local dashboard."""

import asyncio
import hashlib
import io
import json
import os
import sys
import unittest
from types import ModuleType
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit

os.environ["WECHAT_TOKEN"] = "test-only-token"

import app as wechat_app


_TOKEN = "test-only-token"


def _signed_query(**extra: str) -> str:
    timestamp = "1792000000"
    nonce = "test-nonce"
    signature = hashlib.sha1("".join(sorted((_TOKEN, timestamp, nonce))).encode()).hexdigest()
    values = {"timestamp": timestamp, "nonce": nonce, "signature": signature}
    values.update(extra)
    return urlencode(values)


def _message_xml(
    msg_type: str,
    *,
    reader: str = "reader-id-not-for-display",
    official: str = "official-account-id",
    text: str = "你好",
    message_id: str = "12345",
) -> bytes:
    root = ET.Element("xml")
    fields = [
        ("ToUserName", official),
        ("FromUserName", reader),
        ("CreateTime", "1792000000"),
        ("MsgType", msg_type),
    ]
    if msg_type == "text":
        fields.extend((("Content", text), ("MsgId", message_id)))
    else:
        fields.append(("Event", "subscribe"))
    for key, value in fields:
        ET.SubElement(root, key).text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class _ASGIClient:
    def __init__(self, application):
        self.application = application
        self.response_body_sent = False

    def request(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ):
        self.response_body_sent = False
        response_start = {}
        response_body = bytearray()
        sent_request = False
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/xml"),
                *[(key.lower().encode("ascii"), value.encode("utf-8")) for key, value in (headers or {}).items()],
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "state": {},
        }

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response_start.update(message)
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    self.response_body_sent = True
        asyncio.run(self.application(scope, receive, send))
        return _Response(
            status_code=response_start["status"],
            headers={key.decode().lower(): value.decode() for key, value in response_start["headers"]},
            body=bytes(response_body),
        )


class _Response:
    def __init__(self, *, status_code: int, headers: dict[str, str], body: bytes):
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.text = body.decode("utf-8")


class CallbackDashboardTests(unittest.TestCase):
    def setUp(self):
        with wechat_app._MESSAGE_LOCK:
            wechat_app._MESSAGES.clear()
            wechat_app._MESSAGE_IDS.clear()
        with wechat_app._ACCESS_TOKEN_LOCK:
            wechat_app._ACCESS_TOKEN = None
            wechat_app._ACCESS_TOKEN_EXPIRES_AT = 0.0
        self.client = _ASGIClient(wechat_app.app)
        wechat_app.app.state.rag_responder = None


    def test_dashboard_has_valid_message_section(self):
        response = self.client.request("GET", "/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('<section aria-labelledby="messages-title">', response.text)
        self.assertEqual(response.text.count("<section"), response.text.count("</section>"))

    def test_lifespan_keeps_webhook_available_when_rag_initialization_fails(self):
        package = ModuleType("rag_poc")
        package.__path__ = []
        rag_module = ModuleType("rag_poc.cli")

        class BrokenResponder:
            def __init__(self):
                raise RuntimeError("private provider credential detail")

        rag_module.RagResponder = BrokenResponder

        async def start_lifespan():
            async with wechat_app._lifespan(wechat_app.app):
                return None

        with patch.dict(sys.modules, {"rag_poc": package, "rag_poc.cli": rag_module}):
            with self.assertLogs("uvicorn.error", level="ERROR") as captured:
                asyncio.run(start_lifespan())
        self.assertIsNone(wechat_app.app.state.rag_responder)
        self.assertNotIn("private provider credential detail", "\n".join(captured.output))
        response = self.client.request(
            "POST", "/wechat", query=_signed_query(), body=_message_xml("event")
        )
        self.assertEqual((response.status_code, response.text), (200, "success"))

    def test_collection_mode_skips_rag_startup_and_signed_reply(self):
        package = ModuleType("rag_poc")
        package.__path__ = []
        rag_module = ModuleType("rag_poc.cli")
        constructor = patch.object(rag_module, "RagResponder", create=True)

        async def start_lifespan():
            async with wechat_app._lifespan(wechat_app.app):
                self.assertIsNone(wechat_app.app.state.rag_responder)

        with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
            with patch.dict(sys.modules, {"rag_poc": package, "rag_poc.cli": rag_module}):
                with constructor as responder_constructor:
                    asyncio.run(start_lifespan())
                    responder_constructor.assert_not_called()
            with patch.object(wechat_app, "_answer_then_send", new=AsyncMock()) as answer:
                with patch.object(wechat_app, "_send_customer_message") as send:
                    response = self.client.request(
                        "POST",
                        "/wechat",
                        query=_signed_query(),
                        body=_message_xml("text", text="开放时间？", message_id="dataset-1"),
                    )
        self.assertEqual((response.status_code, response.text), (200, "success"))
        answer.assert_not_awaited()
        send.assert_not_called()

    def test_collection_mode_skips_cloud_reply_with_spy_responder(self):
        responder = object()
        body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-id",
                "MsgType": "text",
                "Content": "开放时间？",
                "MsgId": "dataset-1",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
            wechat_app.app.state.rag_responder = responder
            with patch.object(wechat_app.BackgroundTasks, "add_task") as add_task:
                with patch.object(wechat_app, "_send_customer_message") as send:
                    with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
                        response = self.client.request(
                            "POST",
                            "/wechat",
                            body=body,
                            headers={"x-wx-source": "cloudbase", "content-type": "application/json"},
                        )
        self.assertEqual((response.status_code, response.text), (200, "success"))
        add_task.assert_not_called()
        send.assert_not_called()

    def test_collection_mode_hides_debug_query_even_with_token(self):
        with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
            with patch.object(wechat_app, "_RAG_DEBUG_TOKEN", "test-debug-token"):
                response = self.client.request(
                    "POST",
                    "/debug/rag/query",
                    body=b'{"question":"ignored"}',
                    headers={"authorization": "Bearer test-debug-token"},
                )
        self.assertEqual(response.status_code, 404)


    def test_cloud_path_check_accepts_json_without_signature(self):
        with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
            response = self.client.request(
                "POST",
                "/wechat",
                body=b'{"action":"CheckContainerPath"}',
                headers={"x-wx-source": "cloudbase"},
            )
        self.assertEqual((response.status_code, response.text), (200, "success"))

    def test_cloud_header_does_not_bypass_signature_when_cloud_mode_is_disabled(self):
        response = self.client.request(
            "POST",
            "/wechat",
            body=b'{"action":"CheckContainerPath"}',
            headers={"x-wx-source": "spoofed"},
        )
        self.assertEqual(response.status_code, 403)

    def test_cloud_json_text_push_skips_signature_and_sends_once(self):
        answer = "馆藏资料回答。\n\n可核对来源：\n[1] guide.md"

        questions = []

        class Responder:
            def answer(self, question):
                questions.append(question)
                return answer

        wechat_app.app.state.rag_responder = Responder()
        body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-id",
                "MsgType": "text",
                "Content": "开放时间？",
                "MsgId": 987654,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            with patch.object(wechat_app, "_send_customer_message", return_value=True) as send:
                with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
                    response = self.client.request(
                        "POST",
                        "/wechat",
                        body=body,
                        headers={"x-wx-source": "cloudbase", "content-type": "application/json"},
                    )
        self.assertEqual((response.status_code, response.text), (200, "success"))
        logged = "\n".join(captured.output)
        self.assertIn("WeChat callback accepted: type=text transport=cloud", logged)
        self.assertIn("WeChat reply task scheduled:", logged)
        self.assertNotIn("reader-id", logged)
        self.assertNotIn("official-id", logged)
        self.assertNotIn("开放时间？", logged)
        send.assert_called_once_with("reader-id", answer)
        self.assertEqual(questions, ["开放时间？"])

    def test_public_dashboard_can_be_disabled(self):
        with patch.object(wechat_app, "_DASHBOARD_ENABLED", False):
            self.assertEqual(self.client.request("GET", "/").status_code, 404)
            self.assertEqual(self.client.request("GET", "/messages").status_code, 404)

    def test_customer_message_is_bounded_in_utf8_and_keeps_citations(self):
        references = "\n\n可核对来源：\n[1] hours.md — 开放时间"
        original = "开放时间资料说明。" * 500 + references
        bounded = wechat_app._limit_customer_message(original)
        self.assertLessEqual(len(bounded.encode("utf-8")), wechat_app.MAX_CUSTOMER_MESSAGE_BYTES)
        self.assertTrue(bounded.endswith(references))
        self.assertIn("…", bounded)

    def test_oversized_citations_remain_bounded_and_numbered(self):
        references = "\n".join(
            f"[{number}] " + ("馆藏定位信息 — 原文摘录。" * 100)
            for number in range(1, 5)
        )
        bounded = wechat_app._limit_customer_message(
            "回答正文。" * 1000 + "\n\n可核对来源：\n" + references
        )
        self.assertLessEqual(
            len(bounded.encode("utf-8")), wechat_app.MAX_CUSTOMER_MESSAGE_BYTES
        )
        self.assertIn("[1]", bounded)
        self.assertIn("[4]", bounded)
        self.assertIn("可核对来源：", bounded)

    def test_text_callback_runs_rag_then_sends_and_deduplicates(self):
        reader = "reader<&>]]>"
        official = "official<&>]]>"
        text = "合成问题：开放时间是什么？ <script>not HTML</script>"
        answer = "馆藏资料说明开放时间。\n\n可核对来源：\n[1] hours.md — \"开放时间\""
        body = _message_xml("text", reader=reader, official=official, text=text, message_id="rag-1")

        class Responder:
            def __init__(self):
                self.questions = []
                self.acknowledged = False

            def answer(self, question):
                self.acknowledged = client.response_body_sent
                self.questions.append(question)
                return answer

        client = self.client
        responder = Responder()
        wechat_app.app.state.rag_responder = responder
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            with patch.object(wechat_app, "_send_customer_message", return_value=True) as send:
                responses = [
                    self.client.request("POST", "/wechat", query=_signed_query(), body=body)
                    for _ in range(2)
                ]
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "success")
        self.assertTrue(responder.acknowledged)
        self.assertEqual(responder.questions, [text])
        send.assert_called_once_with(reader, answer)
        logged = "\n".join(captured.output)
        scheduled = next(
            line for line in captured.output if "WeChat reply task scheduled:" in line
        )
        record_id = scheduled.rsplit("record_id=", 1)[1]
        self.assertIn("WeChat callback accepted: type=text transport=signed", logged)
        self.assertIn(f"WeChat reply task started: record_id={record_id}", logged)
        self.assertIn(f"RAG reply generation started: record_id={record_id}", logged)
        self.assertIn(f"RAG reply generation completed: record_id={record_id} duration_ms=", logged)
        self.assertIn(f"WeChat reply send started: record_id={record_id}", logged)
        self.assertIn(f"WeChat reply task finished: status=accepted record_id={record_id}", logged)
        self.assertIn("WeChat reply task skipped: reason=duplicate_message", logged)
        self.assertNotIn("rag-1", logged)
        self.assertNotIn("test-nonce", logged)
        self.assertNotIn("private provider", logged)
        self.assertNotIn(reader, logged)
        self.assertNotIn(official, logged)
        self.assertNotIn(text, logged)

        listing = self.client.request("GET", "/messages")
        messages = json.loads(listing.text)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], text)
        self.assertEqual(messages[0]["reply"], answer)
        self.assertEqual(messages[0]["reply_status"], "accepted")
        self.assertNotIn(reader, listing.text)
        self.assertNotIn(official, listing.text)

    def test_text_callback_without_ready_rag_is_not_acknowledged(self):
        wechat_app.app.state.rag_responder = None
        response = self.client.request(
            "POST", "/wechat", query=_signed_query(), body=_message_xml("text")
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(self.client.request("GET", "/messages").text), [])
    def test_rag_stage_failure_logs_safe_code_without_leaking_provider_error(self):
        class StageFailure(Exception):
            code = "llm_synthesis_failed"
            public_message = "LLM 回答生成阶段失败；检查配置。"

        class FailingResponder:
            def answer(self, question):
                raise StageFailure("private provider detail")
        wechat_app.app.state.rag_responder = FailingResponder()
        body = _message_xml("text", text="合成问题", message_id="rag-error")
        with self.assertLogs("uvicorn.error", level="WARNING") as captured:
            with patch.object(wechat_app, "_send_customer_message", return_value=False) as send:
                response = self.client.request(
                    "POST", "/wechat", query=_signed_query(), body=body
                )
        self.assertEqual(response.status_code, 200)
        send.assert_called_once_with(
            "reader-id-not-for-display", wechat_app._RAG_ERROR_REPLY
        )
        logged = "\n".join(captured.output)
        self.assertIn("code=llm_synthesis_failed", logged)
        self.assertIn("error_type=StageFailure", logged)
        self.assertNotIn("private provider detail", logged)
        message = json.loads(self.client.request("GET", "/messages").text)[0]
        self.assertEqual(message["reply"], wechat_app._RAG_ERROR_REPLY)
        self.assertEqual(message["reply_status"], "failed")

    def test_rag_debug_endpoint_requires_token_and_returns_query_answer(self):
        questions = []

        class Responder:
            def answer(self, question):
                questions.append(question)
                return "合成回答\n\n可核对来源：\n[1] hours.md"

        wechat_app.app.state.rag_responder = Responder()
        body = json.dumps({"question": "合成问题：开放时间是什么？"}, ensure_ascii=False).encode()
        with patch.object(wechat_app, "_RAG_DEBUG_TOKEN", ""):
            disabled = self.client.request("POST", "/debug/rag/query", body=body)
        self.assertEqual(disabled.status_code, 404)

        with patch.object(wechat_app, "_RAG_DEBUG_TOKEN", "debug-only-token"):
            unauthorized = self.client.request(
                "POST",
                "/debug/rag/query",
                body=body,
                headers={"authorization": "Bearer wrong-token"},
            )
            self.assertEqual(unauthorized.status_code, 401)

            response = self.client.request(
                "POST",
                "/debug/rag/query",
                body=body,
                headers={"authorization": "Bearer debug-only-token"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                json.loads(response.text),
                {"answer": "合成回答\n\n可核对来源：\n[1] hours.md"},
            )

            oversized = self.client.request(
                "POST",
                "/debug/rag/query",
                body=json.dumps({"question": "x" * (wechat_app.MAX_RAG_DEBUG_QUESTION_CHARS + 1)}).encode(),
                headers={"authorization": "Bearer debug-only-token"},
            )
            self.assertEqual(oversized.status_code, 413)
        self.assertEqual(questions, ["合成问题：开放时间是什么？"])

    def test_rag_debug_endpoint_returns_safe_stage_error_details(self):
        class StageFailure(Exception):
            code = "llm_synthesis_failed"
            public_message = "LLM 回答生成阶段失败；检查 chat API 配置、模型与服务状态。"
            cause_type = "AuthenticationError"

        class FailingResponder:
            def answer(self, question):
                raise StageFailure("private provider endpoint and credential")

        wechat_app.app.state.rag_responder = FailingResponder()
        body = json.dumps({"question": "合成问题"}, ensure_ascii=False).encode()
        with patch.object(wechat_app, "_RAG_DEBUG_TOKEN", "debug-only-token"):
            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                response = self.client.request(
                    "POST",
                    "/debug/rag/query",
                    body=body,
                    headers={"authorization": "Bearer debug-only-token"},
                )

        log_output = "\n".join(captured.output)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            json.loads(response.text),
            {
                "detail": {
                    "code": "llm_synthesis_failed",
                    "message": "LLM 回答生成阶段失败；检查 chat API 配置、模型与服务状态。",
                }
            },
        )
        self.assertIn("code=llm_synthesis_failed", log_output)
        self.assertIn("error_type=AuthenticationError", log_output)
        self.assertNotIn("private provider endpoint and credential", response.text)
        self.assertNotIn("private provider endpoint and credential", log_output)

    def test_customer_message_uses_official_json_and_caches_access_token(self):
        requests = []
        results = iter([
            {"access_token": "test-access-token", "expires_in": 7200},
            {"errcode": 0},
            {"errcode": 0},
        ])
        reply = "测试回答\n\n可核对来源：\n[1] guide.md"

        def fake_request_json(url, payload=None):
            requests.append((url, payload))
            return next(results)

        with patch.object(wechat_app, "_APP_ID", "test-app-id"):
            with patch.object(wechat_app, "_APP_SECRET", "test-app-secret"):
                with patch.object(wechat_app, "_request_json", side_effect=fake_request_json):
                    self.assertTrue(wechat_app._send_customer_message("synthetic-reader", reply))
                    self.assertTrue(wechat_app._send_customer_message("synthetic-reader", reply))

        self.assertEqual(len(requests), 3)
        token_query = parse_qs(urlsplit(requests[0][0]).query)
        self.assertEqual(token_query["grant_type"], ["client_credential"])
        self.assertEqual(token_query["appid"], ["test-app-id"])
        self.assertEqual(token_query["secret"], ["test-app-secret"])
        for url, payload in requests[1:]:
            self.assertEqual(parse_qs(urlsplit(url).query), {"access_token": ["test-access-token"]})
            self.assertEqual(payload, {
                "touser": "synthetic-reader",
                "msgtype": "text",
                "text": {"content": reply},
            })

    def test_open_api_customer_message_omits_access_token(self):
        requests = []

        def fake_request_json(url, payload=None):
            requests.append((url, payload))
            return {"errcode": 0}

        with patch.object(wechat_app, "_WECHAT_OPEN_API_ENABLED", True):
            with patch.object(wechat_app, "_request_json", side_effect=fake_request_json):
                self.assertTrue(wechat_app._send_customer_message("synthetic-reader", "reply"))

        self.assertEqual(requests, [(
            "http://api.weixin.qq.com/cgi-bin/message/custom/send",
            {
                "touser": "synthetic-reader",
                "msgtype": "text",
                "text": {"content": "reply"},
            },
        )])


    def test_api_json_request_uses_post_and_utf8_json(self):
        payload = {
            "touser": "synthetic-reader",
            "msgtype": "text",
            "text": {"content": "RAG 回答\n\n可核对来源：\n[1] guide.md"},
        }
        with patch.object(wechat_app, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{"errcode":0}'
            result = wechat_app._request_json("https://example.invalid/send", payload)

        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 5)
        self.assertEqual(result, {"errcode": 0})

    def test_customer_message_requires_errcode_zero(self):
        with patch.object(wechat_app, "_get_access_token", return_value="test-access-token"):
            with patch.object(wechat_app, "_request_json", return_value={"errcode": 40003}):
                with self.assertLogs("uvicorn.error", level="INFO") as captured:
                    self.assertFalse(wechat_app._send_customer_message("synthetic-reader", "reply"))
        logged = "\n".join(captured.output)
        self.assertIn("WeChat customer message request started: mode=access_token_api", logged)
        self.assertIn("WeChat delayed reply failed: mode=access_token_api errcode=40003", logged)
        self.assertNotIn("synthetic-reader", logged)

    def test_non_text_event_returns_success_without_sending(self):
        with patch.object(wechat_app, "_send_customer_message") as send:
            response = self.client.request(
                "POST", "/wechat", query=_signed_query(), body=_message_xml("event")
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "success")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        send.assert_not_called()
        self.assertEqual(json.loads(self.client.request("GET", "/messages").text), [])

    def test_invalid_signature_is_rejected(self):
        query = urlencode({"timestamp": "1", "nonce": "wrong", "signature": "invalid"})
        response = self.client.request(
            "POST", "/wechat", query=query, body=_message_xml("text")
        )
        self.assertEqual(response.status_code, 403)

    def test_get_signature_echo_semantics_remain_unchanged(self):
        echo = "验证-成功"
        response = self.client.request(
            "GET", "/wechat", query=_signed_query(echostr=echo)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, echo)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")

    def test_recent_message_store_is_bounded_to_one_hundred(self):
        for index in range(wechat_app.MAX_MESSAGES + 1):
            wechat_app._store_message(f"message {index}", f"id-{index}")
        messages = wechat_app._recent_messages()
        self.assertEqual(len(messages), 100)
        self.assertEqual(messages[0]["text"], "message 100")
        self.assertEqual(messages[-1]["text"], "message 1")

    def test_cloud_callback_forwards_text_and_skips_rag_when_auto_reply_disabled(self):
        captured_requests = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok":true}'

        def fake_urlopen(req, timeout=None):
            captured_requests.append((req, timeout))
            return FakeResponse()

        body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-open-id",
                "CreateTime": 1792000123,
                "MsgType": "text",
                "Content": "请问图书馆几点闭馆？",
                "MsgId": 1234567890,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with self.assertLogs("uvicorn.error", level="INFO") as captured_logs:
            with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
                with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
                    with patch.object(
                        wechat_app, "_FORWARD_URL", "https://receiver.invalid/relay/wechat"
                    ):
                        with patch.object(wechat_app, "_FORWARD_TOKEN", "relay-secret-token"):
                            with patch.object(wechat_app, "_FORWARD_TIMEOUT_SECONDS", 2.5):
                                with patch.object(wechat_app, "urlopen", side_effect=fake_urlopen):
                                    response = self.client.request(
                                        "POST",
                                        "/wechat",
                                        body=body,
                                        headers={
                                            "x-wx-source": "cloudbase",
                                            "x-wx-openid": "reader-open-id",
                                            "content-type": "application/json",
                                        },
                                    )

        self.assertEqual((response.status_code, response.text), (200, "success"))
        self.assertEqual(len(captured_requests), 1)
        req, timeout = captured_requests[0]
        self.assertEqual(req.full_url, "https://receiver.invalid/relay/wechat")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, 2.5)
        self.assertEqual(req.get_header("Authorization"), "Bearer relay-secret-token")
        self.assertEqual(req.get_header("User-agent"), "WeChatRelay/1.0")
        forwarded = json.loads(req.data.decode("utf-8"))
        self.assertEqual(forwarded["schema_version"], 1)
        self.assertEqual(forwarded["transport"], "cloud")
        self.assertEqual(forwarded["msg_type"], "text")
        self.assertEqual(forwarded["msg_id"], "1234567890")
        self.assertEqual(forwarded["from_user"], "reader-open-id")
        self.assertEqual(forwarded["to_user"], "official-id")
        self.assertEqual(forwarded["create_time"], "1792000123")
        self.assertEqual(forwarded["content"], "请问图书馆几点闭馆？")
        self.assertEqual(
            forwarded["wx_headers"],
            {"x-wx-source": "cloudbase", "x-wx-openid": "reader-open-id"},
        )
        self.assertEqual(forwarded["raw_body"], body.decode("utf-8"))

        logged = "\n".join(captured_logs.output)
        self.assertIn("WeChat callback forward started: type=text transport=cloud", logged)
        self.assertIn("WeChat callback forward succeeded: status=200", logged)
        self.assertNotIn("relay-secret-token", logged)
        self.assertNotIn("receiver.invalid", logged)
        self.assertNotIn("reader-open-id", logged)
        self.assertNotIn("请问图书馆几点闭馆？", logged)

    def test_cloud_path_check_does_not_forward(self):
        with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
            with patch.object(wechat_app, "_FORWARD_URL", "https://receiver.invalid/relay/wechat"):
                with patch.object(wechat_app, "urlopen") as open_url:
                    response = self.client.request(
                        "POST",
                        "/wechat",
                        body=b'{"action":"CheckContainerPath"}',
                        headers={"x-wx-source": "cloudbase"},
                    )
        self.assertEqual((response.status_code, response.text), (200, "success"))
        open_url.assert_not_called()

    def test_forward_failure_respects_fail_open_flag(self):
        body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-id",
                "MsgType": "event",
                "Event": "subscribe",
                "CreateTime": 1792000001,
            }
        ).encode("utf-8")
        with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
            with patch.object(wechat_app, "_FORWARD_URL", "https://receiver.invalid/relay/wechat"):
                with patch.object(wechat_app, "_FORWARD_FAIL_OPEN", True):
                    with patch.object(wechat_app, "urlopen", side_effect=TimeoutError("timed out")):
                        with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                            response = self.client.request(
                                "POST",
                                "/wechat",
                                body=body,
                                headers={"x-wx-source": "cloudbase"},
                            )
                self.assertEqual((response.status_code, response.text), (200, "success"))
                self.assertIn("error_type=TimeoutError", "\n".join(captured.output))

                http_error = HTTPError(
                    "https://receiver.invalid/relay/wechat",
                    500,
                    "Internal Server Error",
                    hdrs=None,
                    fp=io.BytesIO(b""),
                )
                with patch.object(wechat_app, "_FORWARD_FAIL_OPEN", False):
                    with patch.object(wechat_app, "urlopen", side_effect=http_error):
                        strict_response = self.client.request(
                            "POST",
                            "/wechat",
                            body=body,
                            headers={"x-wx-source": "cloudbase"},
                        )
                self.assertEqual(strict_response.status_code, 502)

    def test_validate_forward_url_rejects_invalid_scheme_or_credentials(self):
        with self.assertRaises(RuntimeError):
            wechat_app._validate_forward_url("ftp://example.com/relay")
        with self.assertRaises(RuntimeError):
            wechat_app._validate_forward_url("https://user:pass@example.com/relay")

    def test_cloud_expected_headers_reject_mismatches_but_allow_container_check(self):
        text_body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-id",
                "MsgType": "text",
                "Content": "你好",
                "MsgId": "hdr-1",
            }
        ).encode("utf-8")
        with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
            with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
                with patch.object(wechat_app, "_CLOUD_EXPECTED_SOURCE", "other"):
                    with patch.object(wechat_app, "_CLOUD_EXPECTED_ENV", "prod-env"):
                        with patch.object(wechat_app, "_CLOUD_EXPECTED_APPID", "wx-app-1"):
                            check = self.client.request(
                                "POST",
                                "/wechat",
                                body=b'{"action":"CheckContainerPath"}',
                                headers={"x-wx-source": "cloudbase"},
                            )
                            self.assertEqual((check.status_code, check.text), (200, "success"))

                            bad = self.client.request(
                                "POST",
                                "/wechat",
                                body=text_body,
                                headers={
                                    "x-wx-source": "other",
                                    "x-wx-env": "wrong-env",
                                    "x-wx-appid": "wx-app-1",
                                },
                            )
                            self.assertEqual(bad.status_code, 403)

                            good = self.client.request(
                                "POST",
                                "/wechat",
                                body=text_body,
                                headers={
                                    "x-wx-source": "other",
                                    "x-wx-env": "prod-env",
                                    "x-wx-appid": "wx-app-1",
                                },
                            )
                            self.assertEqual((good.status_code, good.text), (200, "success"))

    def test_forward_allow_test_reply_sends_only_when_enabled_and_not_duplicate(self):
        class FakeResponse:
            status = 200

            def __init__(self, payload: bytes):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return self._payload

        body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-open-id",
                "MsgType": "text",
                "Content": "测试回复",
                "MsgId": "reply-1",
            },
            ensure_ascii=False,
        ).encode("utf-8")

        with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
            with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
                with patch.object(
                    wechat_app, "_FORWARD_URL", "https://receiver.invalid/relay/wechat"
                ):
                    with patch.object(wechat_app, "_FORWARD_ALLOW_TEST_REPLY", True):
                        with patch.object(
                            wechat_app,
                            "urlopen",
                            return_value=FakeResponse(
                                json.dumps(
                                    {
                                        "ok": True,
                                        "duplicate": False,
                                        "cloud_test_reply": "测试回包",
                                    },
                                    ensure_ascii=False,
                                ).encode("utf-8")
                            ),
                        ):
                            with patch.object(
                                wechat_app, "_send_customer_message", return_value=True
                            ) as send:
                                r1 = self.client.request(
                                    "POST",
                                    "/wechat",
                                    body=body,
                                    headers={"x-wx-source": "other"},
                                )
                        self.assertEqual((r1.status_code, r1.text), (200, "success"))
                        send.assert_called_once_with("reader-open-id", "测试回包")

                        with patch.object(
                            wechat_app,
                            "urlopen",
                            return_value=FakeResponse(
                                json.dumps(
                                    {
                                        "ok": True,
                                        "duplicate": True,
                                        "cloud_test_reply": "测试回包",
                                    },
                                    ensure_ascii=False,
                                ).encode("utf-8")
                            ),
                        ):
                            with patch.object(
                                wechat_app, "_send_customer_message", return_value=True
                            ) as send_dup:
                                r2 = self.client.request(
                                    "POST",
                                    "/wechat",
                                    body=body,
                                    headers={"x-wx-source": "other"},
                                )
                        self.assertEqual((r2.status_code, r2.text), (200, "success"))
                        send_dup.assert_not_called()

    def test_forward_url_error_logs_safe_reason_type_and_errno(self):
        body = json.dumps(
            {
                "ToUserName": "official-id",
                "FromUserName": "reader-id",
                "MsgType": "text",
                "Content": "hi",
                "MsgId": "urlerr-1",
            }
        ).encode("utf-8")
        err = URLError(ConnectionResetError(104, "Connection reset by peer https://secret.invalid"))
        with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
            with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
                with patch.object(
                    wechat_app, "_FORWARD_URL", "https://secret.invalid/relay/wechat"
                ):
                    with patch.object(wechat_app, "_FORWARD_FAIL_OPEN", True):
                        with patch.object(wechat_app, "urlopen", side_effect=err):
                            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                                res = self.client.request(
                                    "POST",
                                    "/wechat",
                                    body=body,
                                    headers={"x-wx-source": "other"},
                                )
        self.assertEqual((res.status_code, res.text), (200, "success"))
        logged = "\n".join(captured.output)
        self.assertIn("error_type=URLError reason_type=ConnectionResetError errno=104", logged)
        self.assertNotIn("secret.invalid", logged)


if __name__ == "__main__":
    unittest.main()
