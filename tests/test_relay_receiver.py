"""Tests for the self-hosted relay receiver and end-to-end forwarding pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn

os.environ.setdefault("WECHAT_TOKEN", "test-only-token")

import app as wechat_app
import relay_receiver
from test_app import _ASGIClient


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RelayReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmpdir.name) / "relay" / "events.jsonl"
        self.client = _ASGIClient(relay_receiver.app)
        relay_receiver.app.state.relay_store = relay_receiver.RelayEventStore(self.log_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_healthz_is_available_without_auth(self):
        response = self.client.request("GET", "/healthz")
        self.assertEqual((response.status_code, response.text), (200, "ok"))

    def test_lifespan_requires_relay_auth_token(self):
        async def start():
            async with relay_receiver._lifespan(relay_receiver.app):
                return None

        with patch.object(relay_receiver, "_AUTH_TOKEN", ""):
            with self.assertRaises(RuntimeError):
                asyncio.run(start())

    def test_post_and_get_require_bearer_token(self):
        payload = json.dumps({"msg_type": "text", "content": "hi"}).encode("utf-8")
        with patch.object(relay_receiver, "_AUTH_TOKEN", "expected-token"):
            missing = self.client.request("POST", "/relay/wechat", body=payload)
            wrong = self.client.request(
                "POST",
                "/relay/wechat",
                body=payload,
                headers={"authorization": "Bearer wrong-token"},
            )
            listing_unauth = self.client.request("GET", "/relay/events")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(listing_unauth.status_code, 401)

    def test_persists_jsonl_with_restrictive_permissions_and_deduplicates_after_reload(self):
        first = json.dumps(
            {
                "schema_version": 1,
                "transport": "cloud",
                "msg_type": "text",
                "msg_id": "msg-100",
                "from_user": "reader-1",
                "to_user": "official-1",
                "create_time": "1792000000",
                "content": "借书可以续借几次？",
                "wx_headers": {"x-wx-source": "cloudbase"},
                "raw_body": '{"Content":"借书可以续借几次？"}',
            },
            ensure_ascii=False,
        ).encode("utf-8")
        event_without_id = json.dumps(
            {
                "schema_version": 1,
                "transport": "cloud",
                "msg_type": "event",
                "event": "subscribe",
                "from_user": "reader-2",
                "wx_headers": {"x-wx-source": "cloudbase"},
            }
        ).encode("utf-8")

        with patch.object(relay_receiver, "_AUTH_TOKEN", "expected-token"):
            auth = {"authorization": "Bearer expected-token"}
            with self.assertLogs("uvicorn.error", level="INFO") as captured_logs:
                r1 = self.client.request("POST", "/relay/wechat", body=first, headers=auth)
                r2 = self.client.request("POST", "/relay/wechat", body=first, headers=auth)
                r3 = self.client.request(
                    "POST", "/relay/wechat", body=event_without_id, headers=auth
                )

            self.assertEqual(json.loads(r1.text), {"ok": True, "accepted": True, "duplicate": False, "seq": 1})
            self.assertEqual(json.loads(r2.text), {"ok": True, "accepted": True, "duplicate": True, "seq": 1})
            self.assertEqual(json.loads(r3.text), {"ok": True, "accepted": True, "duplicate": False, "seq": 2})

            logged = "\n".join(captured_logs.output)
            self.assertIn("wx_source=cloudbase", logged)
            self.assertNotIn("reader-1", logged)
            self.assertNotIn("借书可以续借几次", logged)

            self.assertEqual(stat.S_IMODE(self.log_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.log_path.stat().st_mode), 0o600)
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)

            # Simulate receiver restart and verify deduplication survives reload.
            relay_receiver.app.state.relay_store = relay_receiver.RelayEventStore(self.log_path)
            r4 = self.client.request("POST", "/relay/wechat", body=first, headers=auth)
            self.assertEqual(json.loads(r4.text), {"ok": True, "accepted": True, "duplicate": True, "seq": 1})

            listing = self.client.request(
                "GET", "/relay/events", query="limit=10", headers=auth
            )
            self.assertEqual(listing.status_code, 200)
            listed = json.loads(listing.text)
            self.assertEqual(listed["count"], 2)
            self.assertEqual(listed["events"][0]["seq"], 2)
            self.assertEqual(listed["events"][1]["seq"], 1)
            self.assertEqual(listed["events"][1]["content"], "借书可以续借几次？")

    def test_end_to_end_loopback_forward_from_cloud_callback_to_receiver(self):
        port = _free_tcp_port()
        token = "loopback-pipeline-token"
        cloud_client = _ASGIClient(wechat_app.app)
        wechat_app.app.state.rag_responder = None

        with patch.object(relay_receiver, "_AUTH_TOKEN", token):
            with patch.object(relay_receiver, "_LOG_PATH", self.log_path):
                config = uvicorn.Config(
                    relay_receiver.app,
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                    access_log=False,
                )
                server = uvicorn.Server(config)
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 5.0
                    while not server.started:
                        if time.monotonic() > deadline:
                            self.fail("loopback receiver failed to start in time")
                        time.sleep(0.02)

                    payload = json.dumps(
                        {
                            "ToUserName": "gh_test",
                            "FromUserName": "o_reader_test",
                            "CreateTime": 1792000999,
                            "MsgType": "text",
                            "Content": "云托管转发管道测试消息",
                            "MsgId": "pipe-001",
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")

                    with patch.object(wechat_app, "_CLOUD_PUSH_ENABLED", True):
                        with patch.object(wechat_app, "_AUTO_REPLY_ENABLED", False):
                            with patch.object(
                                wechat_app,
                                "_FORWARD_URL",
                                f"http://127.0.0.1:{port}/relay/wechat",
                            ):
                                with patch.object(wechat_app, "_FORWARD_TOKEN", token):
                                    with patch.object(wechat_app, "_FORWARD_FAIL_OPEN", False):
                                        res = cloud_client.request(
                                            "POST",
                                            "/wechat",
                                            body=payload,
                                            headers={
                                                "x-wx-source": "cloudbase",
                                                "content-type": "application/json",
                                            },
                                        )
                    self.assertEqual((res.status_code, res.text), (200, "success"))
                finally:
                    server.should_exit = True
                    thread.join(timeout=5.0)

        saved_lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(saved_lines), 1)
        event = json.loads(saved_lines[0])
        self.assertEqual(event["msg_type"], "text")
        self.assertEqual(event["msg_id"], "pipe-001")
        self.assertEqual(event["content"], "云托管转发管道测试消息")
        self.assertEqual(event["wx_headers"], {"x-wx-source": "cloudbase"})

    def test_send_test_resolves_seq_caches_token_and_retries_40001(self):
        first = json.dumps(
            {
                "transport": "cloud",
                "msg_type": "text",
                "msg_id": "msg-send-1",
                "from_user": "reader-open-id-xyz",
                "content": "测试发送",
            },
            ensure_ascii=False,
        ).encode("utf-8")

        calls = []
        responses = iter([
            {"access_token": "token-1", "expires_in": 7200},
            {"errcode": 40001, "errmsg": "invalid credential"},
            {"access_token": "token-2", "expires_in": 7200},
            {"errcode": 0, "errmsg": "ok"},
            {"errcode": 48001, "errmsg": "api unauthorized rid: 123"},
        ])

        def fake_request(url, payload=None):
            calls.append((url, payload))
            return next(responses)

        relay_receiver._DIRECT_CLIENT.reset_cache()
        with patch.object(relay_receiver, "_AUTH_TOKEN", "expected-token"):
            with patch.object(relay_receiver, "_APP_ID", "wx-app-id"):
                with patch.object(relay_receiver, "_APP_SECRET", "wx-app-secret"):
                    auth = {"authorization": "Bearer expected-token"}
                    r_in = self.client.request("POST", "/relay/wechat", body=first, headers=auth)
                    self.assertEqual(r_in.status_code, 200)

                    with patch.object(relay_receiver, "_request_json", side_effect=fake_request):
                        with self.assertLogs("uvicorn.error", level="INFO") as captured_logs:
                            r_ok = self.client.request(
                                "POST",
                                "/relay/send-test",
                                body=json.dumps({"seq": 1, "text": "第一条测试回复"}).encode("utf-8"),
                                headers=auth,
                            )
                            r_48001 = self.client.request(
                                "POST",
                                "/relay/send-test",
                                body=json.dumps({"seq": 1, "text": "第二条测试回复"}).encode("utf-8"),
                                headers=auth,
                            )
                        r_missing = self.client.request(
                            "POST",
                            "/relay/send-test",
                            body=json.dumps({"seq": 999, "text": "不存在"}).encode("utf-8"),
                            headers=auth,
                        )

        self.assertEqual(r_ok.status_code, 200)
        ok_body = json.loads(r_ok.text)
        self.assertTrue(ok_body["ok"])
        self.assertEqual(ok_body["errcode"], 0)
        self.assertEqual(ok_body["seq"], 1)

        self.assertEqual(r_48001.status_code, 200)
        err_body = json.loads(r_48001.text)
        self.assertFalse(err_body["ok"])
        self.assertEqual(err_body["errcode"], 48001)
        self.assertIn("api unauthorized", err_body["errmsg"])

        self.assertEqual(r_missing.status_code, 404)
        self.assertEqual(len(calls), 5)
        self.assertIn("access_token=token-2", calls[4][0])
        logged = "\n".join(captured_logs.output)
        self.assertNotIn("reader-open-id-xyz", logged)
        self.assertNotIn("wx-app-secret", logged)
        self.assertNotIn("token-2", logged)

    def test_direct_echo_and_cloud_echo_test_hooks(self):
        payload = json.dumps(
            {
                "transport": "cloud",
                "msg_type": "text",
                "msg_id": "echo-1",
                "from_user": "reader-echo",
                "content": "测试回声",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with patch.object(relay_receiver, "_AUTH_TOKEN", "expected-token"):
            with patch.object(relay_receiver, "_DIRECT_ECHO_TEXT", "直连回声"):
                with patch.object(relay_receiver, "_CLOUD_ECHO_TEXT", "云端回声"):
                    with patch.object(
                        relay_receiver._DIRECT_CLIENT,
                        "send_text",
                        return_value={
                            "ok": True,
                            "errcode": 0,
                            "errmsg": "ok",
                            "duration_ms": 12,
                            "mode": "direct_https",
                        },
                    ) as send_mock:
                        auth = {"authorization": "Bearer expected-token"}
                        r1 = self.client.request("POST", "/relay/wechat", body=payload, headers=auth)
                        r2 = self.client.request("POST", "/relay/wechat", body=payload, headers=auth)

        b1 = json.loads(r1.text)
        b2 = json.loads(r2.text)
        self.assertEqual(b1.get("cloud_test_reply"), "云端回声")
        self.assertNotIn("cloud_test_reply", b2)
        send_mock.assert_called_once_with("reader-echo", "直连回声")

    def test_cloud_expected_headers_verify_forwarded_wx_headers(self):
        base = {
            "transport": "cloud",
            "msg_type": "text",
            "msg_id": "hdr-check-1",
            "from_user": "reader-1",
            "content": "测试来源头校验",
        }
        missing_headers = json.dumps(base, ensure_ascii=False).encode("utf-8")
        mismatched = json.dumps(
            {
                **base,
                "wx_headers": {
                    "x-wx-source": "other",
                    "x-wx-env": "wrong-env",
                    "x-wx-appid": "wx-app-1",
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        matched = json.dumps(
            {
                **base,
                "wx_headers": {
                    "x-wx-source": "other",
                    "x-wx-env": "prod-env",
                    "x-wx-appid": "wx-app-1",
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")

        with patch.object(relay_receiver, "_AUTH_TOKEN", "expected-token"):
            with patch.object(relay_receiver, "_CLOUD_EXPECTED_SOURCE", "other"):
                with patch.object(relay_receiver, "_CLOUD_EXPECTED_ENV", "prod-env"):
                    with patch.object(relay_receiver, "_CLOUD_EXPECTED_APPID", "wx-app-1"):
                        auth = {"authorization": "Bearer expected-token"}
                        with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                            r_missing = self.client.request(
                                "POST", "/relay/wechat", body=missing_headers, headers=auth
                            )
                            r_bad = self.client.request(
                                "POST", "/relay/wechat", body=mismatched, headers=auth
                            )
                        r_ok = self.client.request(
                            "POST", "/relay/wechat", body=matched, headers=auth
                        )

        self.assertEqual(r_missing.status_code, 403)
        self.assertEqual(r_bad.status_code, 403)
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(json.loads(r_ok.text)["seq"], 1)
        logged = "\n".join(captured.output)
        self.assertIn("reason=header_mismatch header=x-wx-source", logged)
        self.assertIn("reason=header_mismatch header=x-wx-env", logged)
        self.assertEqual(len(self.log_path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
