"""End-to-end route tests for Step 5: Session/RBAC auth, direct staff replies, and dual-reviewed presets."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI

from support_backend import (
    PresetCatalog,
    SupportPipeline,
    SupportResponder,
    SupportStore,
)
from support_backend.dashboard import create_dashboard_router
from support_backend.filters import FilterDecision


class _CookieASGIClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def request(
        self,
        method: str,
        target: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        scheme: str = "https",
    ) -> SimpleNamespace:
        parsed = urlsplit(target)
        path = parsed.path or "/"
        raw_headers = [(b"host", b"dashboard.example")]
        raw_headers.extend(
            (name.lower().encode("ascii"), value.encode("utf-8"))
            for name, value in (headers or {}).items()
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("dashboard.example", 443 if scheme == "https" else 80),
            "state": {},
        }
        response_start: dict[str, object] = {}
        response_body = bytearray()
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response_start.update(message)
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        asyncio.run(self.app(scope, receive, send))
        raw_hdrs = response_start.get("headers", [])
        response_headers: dict[str, str] = {}
        set_cookies: list[str] = []
        for k, v in raw_hdrs:
            key = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            if key == "set-cookie":
                set_cookies.append(val)
            response_headers[key] = val
        return SimpleNamespace(
            status_code=response_start["status"],
            headers=response_headers,
            set_cookies=set_cookies,
            text=response_body.decode("utf-8"),
        )


def _extract_cookie(set_cookies: list[str], name: str) -> str:
    prefix = f"{name}="
    for raw in set_cookies:
        if raw.startswith(prefix):
            return raw.split(";", 1)[0]
    return ""


class Step5EndToEndRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.static_corpus = self.root / "static_corpus"
        self.dynamic_presets = self.root / "dynamic_presets"
        self.static_corpus.mkdir()
        (self.static_corpus / "1. 开馆时间.md").write_text(
            '---\nid: "preset_hours"\ntitle: "开馆时间"\naliases:\n  - "开馆时间"\napplicability: "查询开馆时间。"\n---\n每日 08:00-22:30 开放。\n',
            encoding="utf-8",
        )

        self.now = [1_800_000_000.0]
        self.store = SupportStore(self.root / "support.db", clock=lambda: self.now[0])
        self.sent_messages: list[tuple[str, str]] = []
        self.send_should_fail = False

        def fake_send(to_user: str, content: str) -> dict[str, object]:
            if self.send_should_fail:
                return {"ok": False, "errcode": 45015, "errmsg": "response time out", "duration_ms": 12}
            self.sent_messages.append((to_user, content))
            return {"ok": True, "errcode": 0, "errmsg": "ok", "duration_ms": 15}

        self.catalog = PresetCatalog.from_corpus_dirs(self.static_corpus, self.dynamic_presets)
        self.responder = SupportResponder(
            store=self.store,
            preset_catalog=self.catalog,
            corpus_dir=self.static_corpus,
            dynamic_presets_dir=self.dynamic_presets,
        )
        self.pipeline = SupportPipeline(
            self.store,
            send_text_fn=fake_send,
            auto_reply_enabled=False,  # Ensure staff direct reply bypasses auto-reply flag
        )

        self.ai_audit_pass = True

        def fake_generator(sys_p: str, user_p: str) -> dict[str, object]:
            return {
                "mode": "create",
                "target_preset_id": "",
                "preset_id": "preset_silent_pod",
                "title": "静音舱预约",
                "aliases": ["静音舱", "静音舱怎么预约"],
                "applicability": "适用于读者咨询图书馆静音舱预约规则与使用时长，不适用于设备故障报修。",
                "body": "静音舱可通过图书馆预约系统提前 1 天预约，每次最长使用 2 小时。",
                "note": "已脱敏整理为通用静音舱预约规范。",
            }

        def fake_auditor(sys_p: str, user_p: str) -> dict[str, object]:
            if not self.ai_audit_pass:
                return {
                    "passed": False,
                    "reasons": ["该内容与已有模板范畴重合，建议并入。"],
                    "suggestion": "请将模式改为并入现有模板。",
                    "suggested_mode": "merge",
                    "suggested_target_id": "preset_hours",
                }
            return {
                "passed": True,
                "reasons": [],
                "suggestion": "",
                "suggested_mode": "",
                "suggested_target_id": "",
            }

        self.app = FastAPI()
        self.app.include_router(
            create_dashboard_router(
                admin_password="admin-secret-password",
                librarian_password="",
                public_origin="https://dashboard.example",
                preset_generator_llm_fn=fake_generator,
                preset_auditor_llm_fn=fake_auditor,
            )
        )
        self.app.state.support_pipeline = self.pipeline
        self.app.state.support_responder = self.responder
        self.client = _CookieASGIClient(self.app)
        self.same_origin = {
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://dashboard.example",
            "sec-fetch-site": "same-origin",
        }

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def _login(self, username: str, password: str) -> str:
        resp = self.client.request(
            "POST",
            "/dashboard/login",
            body=urlencode({"username": username, "password": password}).encode(),
            headers=self.same_origin,
        )
        self.assertEqual(resp.status_code, 303)
        cookie = _extract_cookie(resp.set_cookies, "library_dashboard_session")
        self.assertTrue(cookie)
        return cookie

    def test_cookie_login_rbac_user_management_and_immediate_revocation(self):
        # Browser GET without auth redirects to login page
        redir = self.client.request("GET", "/dashboard/librarian", headers={"accept": "text/html"})
        self.assertEqual(redir.status_code, 303)
        self.assertIn("/dashboard/login?next=", redir.headers["location"])

        # Login as env bootstrap admin
        admin_cookie = self._login("admin", "admin-secret-password")

        # Create a read-only librarian
        create_ro = self.client.request(
            "POST",
            "/dashboard/admin/users/create",
            body=urlencode(
                [
                    ("username", "alice_ro"),
                    ("display_name", "爱丽丝"),
                    ("password", "alice-pass-123"),
                    ("role", "librarian"),
                    ("perm", "view_workbench"),
                ]
            ).encode(),
            headers={**self.same_origin, "cookie": admin_cookie},
        )
        self.assertEqual(create_ro.status_code, 303)
        self.assertIn("result=created", create_ro.headers["location"])

        # Alice logs in via web form
        alice_cookie = self._login("alice_ro", "alice-pass-123")
        wb = self.client.request("GET", "/dashboard/librarian", headers={"cookie": alice_cookie})
        self.assertEqual(wb.status_code, 200)
        self.assertIn("爱丽丝", wb.text)
        # Cannot access admin or presets or reply
        self.assertEqual(
            self.client.request("GET", "/dashboard/admin", headers={"cookie": alice_cookie}).status_code,
            403,
        )
        self.assertEqual(
            self.client.request("GET", "/dashboard/presets", headers={"cookie": alice_cookie}).status_code,
            403,
        )
        self.assertEqual(
            self.client.request(
                "POST",
                "/dashboard/librarian/cases/1/reply",
                body=urlencode({"reply_text": "hello"}).encode(),
                headers={**self.same_origin, "cookie": alice_cookie},
            ).status_code,
            403,
        )

        # Admin disables Alice -> Alice's active session immediately becomes invalid
        disable = self.client.request(
            "POST",
            "/dashboard/admin/users/alice_ro/update",
            body=urlencode({"display_name": "爱丽丝", "role": "librarian", "is_active": "0"}).encode(),
            headers={**self.same_origin, "cookie": admin_cookie},
        )
        self.assertEqual(disable.status_code, 303)
        after_disable = self.client.request("GET", "/dashboard/librarian", headers={"cookie": alice_cookie})
        self.assertEqual(after_disable.status_code, 401)

    def test_direct_reply_open_closed_inbox_and_dual_review_preset_hot_reload(self):
        # Seed a reader inbound message (opens 48h window) and a staff case
        ev = self.store.ingest_event(
            {
                "to_user": "gh_lib",
                "from_user": "o_reader_live_1",
                "msg_type": "text",
                "msg_id": "m_live_1",
                "content": "静音舱怎么预约呀？",
            },
            FilterDecision(category="question", reason="q", normalized_text="静音舱怎么预约呀"),
        )
        case = self.store.create_staff_case(
            account_id="gh_lib",
            reader_id="o_reader_live_1",
            summary="静音舱预约咨询",
            latest_message="静音舱怎么预约呀？",
            source_event_seq=ev.seq,
        )

        admin_cookie = self._login("admin", "admin-secret-password")
        # Create a frontline librarian with reply + close + draft_preset (NO review_preset)
        self.store.create_dashboard_user(
            username="bob_staff",
            password="bob-password-123",
            display_name="鲍勃",
            role="librarian",
            permissions=("view_workbench", "reply_reader", "close_case", "draft_preset"),
        )
        bob_cookie = self._login("bob_staff", "bob-password-123")

        # 1) Send failure must NOT change case status
        self.send_should_fail = True
        fail_resp = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/reply",
            body=urlencode({"reply_text": "先回复试试"}).encode(),
            headers={**self.same_origin, "cookie": bob_cookie},
        )
        self.assertEqual(fail_resp.status_code, 303)
        self.assertIn("result=send-failed", fail_resp.headers["location"])
        self.assertEqual(len(self.store.list_staff_cases(status="open")), 1)

        # 2) Successful single-button reply marks case as `replied` and auto-scans preset draft in background
        self.send_should_fail = False
        ok_resp = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/reply",
            body=urlencode(
                {
                    "reply_text": "同学您好，静音舱可以通过预约系统提前一天预约，每次两小时。",
                }
            ).encode(),
            headers={**self.same_origin, "cookie": bob_cookie},
        )
        self.assertEqual(ok_resp.status_code, 303)
        self.assertIn("result=replied", ok_resp.headers["location"])
        self.assertEqual(len(self.sent_messages), 1)
        self.assertEqual(self.sent_messages[0][0], "o_reader_live_1")
        self.assertEqual(len(self.store.list_staff_cases(status="open")), 0)
        self.assertEqual(len(self.store.list_staff_cases(status="replied")), 1)

        # 3) Default librarian view hides replied/closed cases; ?show=all reveals it with 「已处理 · 等待读者」
        default_view = self.client.request("GET", "/dashboard/librarian?view=cases", headers={"cookie": bob_cookie})
        self.assertNotIn(f"/dashboard/librarian/cases/{case.case_id}/reply", default_view.text)
        all_view = self.client.request("GET", "/dashboard/librarian?view=cases&show=all", headers={"cookie": bob_cookie})
        self.assertIn("静音舱预约咨询", all_view.text)
        self.assertIn("已处理 · 等待读者", all_view.text)
        self.assertIn(f"/dashboard/librarian/cases/{case.case_id}/reply", all_view.text)
        self.assertNotIn("o_reader_live_1", all_view.text)

        # Follow-up reply on a `replied` case also succeeds within 48h window without duplicating active draft
        followup = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/reply",
            body=urlencode({"reply_text": "补充说明：请提前10分钟签到。", "show": "all"}).encode(),
            headers={**self.same_origin, "cookie": bob_cookie},
        )
        self.assertEqual(followup.status_code, 303)
        self.assertIn("result=replied", followup.headers["location"])
        self.assertEqual(len(self.sent_messages), 2)

        # Replying directly on 48h inbox event also works (and does not duplicate active case draft)
        inbox_reply = self.client.request(
            "POST",
            f"/dashboard/librarian/inbox/{ev.seq}/reply",
            body=urlencode({"reply_text": "私信直接补答测试"}).encode(),
            headers={**self.same_origin, "cookie": bob_cookie},
        )
        self.assertEqual(inbox_reply.status_code, 303)
        self.assertIn("result=replied", inbox_reply.headers["location"])
        self.assertEqual(len(self.sent_messages), 3)

        # 4) Preset draft workflow: Bob cannot publish (403); Admin AI-rejected first, then passes & hot-reloads!
        drafts = self.store.list_preset_drafts(status="draft")
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        did = draft["draft_id"]

        publish_payload = urlencode(
            {
                "mode": "create",
                "target_preset_id": "",
                "preset_id": "preset_silent_pod",
                "title": "静音舱预约",
                "aliases": "静音舱\n静音舱怎么预约",
                "applicability": "适用于读者咨询图书馆静音舱预约规则与使用时长，不适用于设备故障报修。",
                "body": "静音舱可通过图书馆预约系统提前 1 天预约，每次最长使用 2 小时。",
                "intent": "publish",
            }
        ).encode()
        bob_pub = self.client.request(
            "POST",
            f"/dashboard/presets/drafts/{did}/save",
            body=publish_payload,
            headers={**self.same_origin, "cookie": bob_cookie},
        )
        self.assertEqual(bob_pub.status_code, 403)

        # Deterministic PII rejection blocks publish even for admin
        pii_payload = urlencode(
            {
                "mode": "create",
                "target_preset_id": "",
                "preset_id": "preset_silent_pod",
                "title": "静音舱预约",
                "aliases": "静音舱\n静音舱怎么预约",
                "applicability": "适用于静音舱预约咨询。",
                "body": "已帮您预约成功，请联系手机号 13800138000。",
                "intent": "publish",
            }
        ).encode()
        pii_rej = self.client.request(
            "POST",
            f"/dashboard/presets/drafts/{did}/save",
            body=pii_payload,
            headers={**self.same_origin, "cookie": admin_cookie},
        )
        self.assertEqual(pii_rej.status_code, 303)
        self.assertIn("result=ai-rejected", pii_rej.headers["location"])
        self.assertIsNone(self.responder.preset_catalog.match_exact("静音舱"))

        # Independent AI review rejection blocks publish too
        self.ai_audit_pass = False
        ai_rej = self.client.request(
            "POST",
            f"/dashboard/presets/drafts/{did}/save",
            body=publish_payload,
            headers={**self.same_origin, "cookie": admin_cookie},
        )
        self.assertEqual(ai_rej.status_code, 303)
        self.assertIn("result=ai-rejected", ai_rej.headers["location"])
        self.assertIsNone(self.responder.preset_catalog.match_exact("静音舱"))

        # AI review passes -> dynamic file written & PresetCatalog hot-reloaded!
        self.ai_audit_pass = True
        pub_ok = self.client.request(
            "POST",
            f"/dashboard/presets/drafts/{did}/save",
            body=publish_payload,
            headers={**self.same_origin, "cookie": admin_cookie},
        )
        self.assertEqual(pub_ok.status_code, 303)
        self.assertIn("result=published", pub_ok.headers["location"])

        hit = self.responder.preset_catalog.match_exact("静音舱怎么预约？")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.preset_id, "preset_silent_pod")
        self.assertTrue(hit.is_dynamic)

        # 5) 49h later: old 12h session expired (401), and after re-login 48h window blocks replies
        self.now[0] += 49 * 3600
        self.assertEqual(
            self.client.request("GET", "/dashboard/librarian", headers={"cookie": bob_cookie}).status_code,
            401,
        )
        bob_cookie = self._login("bob_staff", "bob-password-123")
        expired = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/reply",
            body=urlencode({"reply_text": "超时回复"}).encode(),
            headers={**self.same_origin, "cookie": bob_cookie},
        )
        self.assertEqual(expired.status_code, 303)
        self.assertIn("result=quota-blocked", expired.headers["location"])
        self.assertEqual(len(self.sent_messages), 3)


if __name__ == "__main__":
    unittest.main()
