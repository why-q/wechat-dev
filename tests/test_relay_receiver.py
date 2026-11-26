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


if __name__ == "__main__":
    unittest.main()
