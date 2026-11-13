"""Consumer-facing tests for the plaintext callback and local dashboard."""

import asyncio
import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET
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

    def request(self, method: str, path: str, *, query: str = "", body: bytes = b""):
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
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/xml")],
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

    def test_text_callback_acknowledges_then_sends_and_deduplicates(self):
        reader = "reader<&>]]>"
        official = "official<&>]]>"
        text = "内容 & 小于 < 大于 > 结尾 ]]> <img src=x onerror=alert(1)>"
        body = _message_xml("text", reader=reader, official=official, text=text, message_id="retry-1")

        async def check_delay_after_ack(delay):
            self.assertTrue(self.client.response_body_sent)
            self.assertEqual(delay, 10)

        sleep = AsyncMock(side_effect=check_delay_after_ack)
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            with patch.object(wechat_app.asyncio, "sleep", new=sleep):
                with patch.object(wechat_app, "_send_customer_message", return_value=True) as send:
                    responses = [
                        self.client.request("POST", "/wechat", query=_signed_query(), body=body)
                        for _ in range(2)
                    ]
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "success")
            self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        sleep.assert_awaited_once_with(10)
        send.assert_called_once_with(reader, "已收到你的消息，测试号自动回复演示已接通。")

        logged = "\n".join(captured.output)
        self.assertNotIn(reader, logged)
        self.assertNotIn(official, logged)
        self.assertNotIn(text, logged)

        page = self.client.request("GET", "/")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.headers["content-type"].startswith("text/html"))
        self.assertIn("incomingText.textContent = message.text", page.text)
        self.assertIn("replyText.textContent = message.reply", page.text)
        self.assertIn("message.reply_status", page.text)
        self.assertNotIn("<img src=x onerror=", page.text)
        listing = self.client.request("GET", "/messages")
        self.assertEqual(listing.status_code, 200)
        messages = json.loads(listing.text)
        self.assertEqual(len(messages), 1, "a repeated MsgId must not duplicate the dashboard row or send")
        self.assertEqual(messages[0]["text"], text)
        self.assertEqual(messages[0]["reply"], "已收到你的消息，测试号自动回复演示已接通。")
        self.assertEqual(messages[0]["reply_status"], "accepted")
        self.assertEqual(set(messages[0]), {"timestamp", "text", "reply", "reply_status"})
        self.assertNotIn("MsgId", listing.text)
        self.assertNotIn(reader, listing.text)
        self.assertNotIn(official, listing.text)

    def test_customer_message_uses_official_json_and_caches_access_token(self):
        requests = []
        results = iter([
            {"access_token": "test-access-token", "expires_in": 7200},
            {"errcode": 0},
            {"errcode": 0},
        ])

        def fake_request_json(url, payload=None):
            requests.append((url, payload))
            return next(results)

        with patch.object(wechat_app, "_APP_ID", "test-app-id"):
            with patch.object(wechat_app, "_APP_SECRET", "test-app-secret"):
                with patch.object(wechat_app, "_request_json", side_effect=fake_request_json):
                    self.assertTrue(wechat_app._send_customer_message("synthetic-reader", wechat_app._REPLY_TEXT))
                    self.assertTrue(wechat_app._send_customer_message("synthetic-reader", wechat_app._REPLY_TEXT))

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
                "text": {"content": "已收到你的消息，测试号自动回复演示已接通。"},
            })


    def test_api_json_request_uses_post_and_utf8_json(self):
        payload = {
            "touser": "synthetic-reader",
            "msgtype": "text",
            "text": {"content": "已收到你的消息，测试号自动回复演示已接通。"},
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
                with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                    self.assertFalse(wechat_app._send_customer_message("synthetic-reader", "reply"))
        self.assertIn("errcode=40003", captured.output[0])
        self.assertNotIn("synthetic-reader", captured.output[0])

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


if __name__ == "__main__":
    unittest.main()
