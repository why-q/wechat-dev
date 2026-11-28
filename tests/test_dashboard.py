"""Isolated route tests for the role-protected live dashboard."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI

from support_backend import SupportPipeline, SupportStore
from support_backend.dashboard import create_dashboard_router, _overview_panels
from support_backend.filters import FilterDecision


class _ASGIClient:
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
        response_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in response_start.get("headers", [])
        }
        return SimpleNamespace(
            status_code=response_start["status"],
            headers=response_headers,
            text=response_body.decode("utf-8"),
        )


def _basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SupportStore(Path(self.tempdir.name) / "support.sqlite")
        self.pipeline = SupportPipeline(self.store)
        self.app = FastAPI()
        self.app.include_router(
            create_dashboard_router(
                admin_password="admin-test-password",
                librarian_password="librarian-test-password",
                public_origin="https://dashboard.example",
            )
        )
        self.app.state.support_pipeline = self.pipeline
        self.client = _ASGIClient(self.app)
        for index in range(51):
            content = "Reader 50 <script>alert(1)</script>" if index == 50 else f"Reader {index} question"
            self.store.ingest_event(
                {
                    "to_user": "official-account",
                    "from_user": f"reader-{index}",
                    "msg_type": "text",
                    "msg_id": f"message-{index}",
                    "content": content,
                },
                FilterDecision(category="question", reason="test", normalized_text=content),
            )
        self.store.record_outbound_reply(
            account_id="official-account",
            reader_id="reader-0",
            trigger_kind="question",
            trigger_ref_id=1,
            content="sample shadow reply",
            delivery_mode="shadow",
            status="shadow",
        )
        self.store.record_outbound_reply(
            account_id="official-account",
            reader_id="reader-1",
            trigger_kind="question",
            trigger_ref_id=2,
            content="sample sent reply",
            delivery_mode="direct_https",
            status="sent",
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def _request_headers(self, role: str, password: str) -> dict[str, str]:
        return {"authorization": _basic_auth(role, password)}

    def test_role_permissions_real_summary_and_cache_headers(self):
        librarian = self.client.request(
            "GET",
            "/dashboard/librarian",
            headers=self._request_headers("librarian", "librarian-test-password"),
        )
        self.assertEqual(librarian.status_code, 200)
        self.assertIn("Reader 50 &lt;script&gt;alert(1)&lt;/script&gt;", librarian.text)
        self.assertNotIn("<script>alert(1)</script>", librarian.text)
        self.assertEqual(librarian.text.count('class="case-state">未结案'), 50)
        self.assertNotIn('href="/dashboard/admin"', librarian.text)
        self.assertEqual(librarian.headers["cache-control"], "no-store")
        self.assertEqual(librarian.headers["referrer-policy"], "strict-origin")
        self.assertEqual(len(self.store.list_librarian_inbox(limit=200)), 51)

        denied = self.client.request(
            "GET",
            "/dashboard/admin",
            headers=self._request_headers("librarian", "librarian-test-password"),
        )
        self.assertEqual(denied.status_code, 403)

        self.store.enroll_allowlist(
            account_id="official-account",
            reader_id="openid-demo-1",
            source="admin",
        )
        admin = self.client.request(
            "GET",
            "/dashboard/admin",
            headers=self._request_headers("admin", "admin-test-password"),
        )
        self.assertEqual(admin.status_code, 200)
        self.assertIn("51", admin.text)
        self.assertIn("影子模式", admin.text)
        self.assertIn("自动回复已关闭", admin.text)
        self.assertIn('href="/dashboard/librarian"', admin.text)
        self.assertIn("微信接口已受理", admin.text)
        self.assertIn("1,234", _overview_panels({"inbound_categories": {"question": 1234}}))

        insecure = self.client.request(
            "GET",
            "/dashboard/admin",
            headers=self._request_headers("admin", "admin-test-password"),
            scheme="http",
        )
        self.assertEqual(insecure.status_code, 404)

    def test_pagination_and_admin_allowlist_mutations_are_scoped_and_csrf_checked(self):
        older = self.client.request(
            "GET",
            "/dashboard/librarian?before=2",
            headers=self._request_headers("admin", "admin-test-password"),
        )
        self.assertEqual(older.status_code, 200)
        self.assertIn("Reader 0 question", older.text)
        self.assertNotIn("Reader 50", older.text)
        self.assertEqual(older.text.count('class="case-state">未结案'), 1)

        body = urlencode({"account_id": "account-a", "reader_id": "openid-a"}).encode()
        base_headers = {
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://dashboard.example",
            "sec-fetch-site": "same-origin",
        }
        librarian_add = self.client.request(
            "POST",
            "/dashboard/admin/allowlist/add",
            body=body,
            headers={**base_headers, **self._request_headers("librarian", "librarian-test-password")},
        )
        self.assertEqual(librarian_add.status_code, 403)
        self.assertFalse(self.store.is_allowlisted("account-a", "openid-a"))

        cross_origin = self.client.request(
            "POST",
            "/dashboard/admin/allowlist/add",
            body=body,
            headers={**base_headers, "origin": "https://attacker.example", **self._request_headers("admin", "admin-test-password")},
        )
        self.assertEqual(cross_origin.status_code, 403)
        self.assertFalse(self.store.is_allowlisted("account-a", "openid-a"))

        added = self.client.request(
            "POST",
            "/dashboard/admin/allowlist/add",
            body=body,
            headers={**base_headers, **self._request_headers("admin", "admin-test-password")},
        )
        self.assertEqual(added.status_code, 303)
        self.assertTrue(self.store.is_allowlisted("account-a", "openid-a"))
        entries = self.store.list_allowlist(limit=200)
        self.assertEqual(entries[0]["source"], "admin")

        self.store.enroll_allowlist(account_id="account-b", reader_id="openid-a", source="keyword")
        remove = self.client.request(
            "POST",
            "/dashboard/admin/allowlist/remove",
            body=urlencode({"account_id": "account-a", "reader_id": "openid-a", "confirm": "yes"}).encode(),
            headers={**base_headers, **self._request_headers("admin", "admin-test-password")},
        )
        self.assertEqual(remove.status_code, 303)
        self.assertFalse(self.store.is_allowlisted("account-a", "openid-a"))
        self.assertTrue(self.store.is_allowlisted("account-b", "openid-a"))

        self.store.enroll_allowlist(account_id="account-b", reader_id="openid-b", source="admin")
        self.store.enroll_allowlist(account_id="account-c", reader_id="openid-c", source="admin")
        batch_body = urlencode(
            [
                ("confirm", "yes"),
                ("item", "account-b\topenid-a"),
                ("item", "account-b\topenid-b"),
            ]
        ).encode()
        batch_denied = self.client.request(
            "POST",
            "/dashboard/admin/allowlist/remove-batch",
            body=batch_body,
            headers={**base_headers, **self._request_headers("librarian", "librarian-test-password")},
        )
        self.assertEqual(batch_denied.status_code, 403)
        batch_ok = self.client.request(
            "POST",
            "/dashboard/admin/allowlist/remove-batch",
            body=batch_body,
            headers={**base_headers, **self._request_headers("admin", "admin-test-password")},
        )
        self.assertEqual(batch_ok.status_code, 303)
        self.assertEqual(batch_ok.headers["location"], "/dashboard/admin?result=batch-removed")
        self.assertFalse(self.store.is_allowlisted("account-b", "openid-a"))
        self.assertFalse(self.store.is_allowlisted("account-b", "openid-b"))
        self.assertTrue(self.store.is_allowlisted("account-c", "openid-c"))

    def test_missing_credentials_fail_closed(self):
        app = FastAPI()
        app.include_router(create_dashboard_router(public_origin="https://dashboard.example"))
        client = _ASGIClient(app)
        response = client.request("GET", "/dashboard/admin")
        self.assertEqual(response.status_code, 404)

    def test_live_charts_inline_css_and_librarian_case_closure_flow(self):
        case = self.store.create_staff_case(
            account_id="official-account",
            reader_id="openid-secret-case",
            summary="借阅证遗失补办",
            latest_message="请问借阅证丢了怎么挂失？",
            case_kind="staff_todo",
        )
        self.store.ingest_event(
            {
                "to_user": "official-account",
                "from_user": "openid-secret-fb",
                "msg_type": "text",
                "msg_id": "fb-msg-1",
                "content": "建议三楼增加电源插座",
            },
            FilterDecision(category="feedback", reason="suggestion", normalized_text="建议三楼增加电源插座"),
        )
        self.store.record_triage_run(
            batch_id=1,
            account_id="official-account",
            reader_id="reader-0",
            generation=1,
            clarified_already=False,
            stage_path="tier2_short",
            reply_preview="短答",
        )

        self.store.enroll_allowlist(
            account_id="official-account",
            reader_id="openid-demo-1",
            source="admin",
        )
        admin = self.client.request(
            "GET",
            "/dashboard/admin",
            headers=self._request_headers("admin", "admin-test-password"),
        )
        self.assertEqual(admin.status_code, 200)
        self.assertIn("<style>", admin.text)
        self.assertIn("近 7 日入站趋势", admin.text)
        self.assertIn("问答分流路径", admin.text)
        self.assertIn("<svg", admin.text)
        self.assertIn("script-src 'sha256-", admin.headers["content-security-policy"])
        self.assertIn("allowlist-select-all", admin.text)
        self.assertIn("trash-confirm", admin.text)

        lib_csp = self.client.request(
            "GET",
            "/dashboard/librarian",
            headers=self._request_headers("librarian", "librarian-test-password"),
        )
        self.assertIn("script-src 'none'", lib_csp.headers["content-security-policy"])

        css = self.client.request("GET", "/dashboard/static/dashboard.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn(".view-tabs", css.text)

        lib_cases = self.client.request(
            "GET",
            "/dashboard/librarian?view=cases",
            headers=self._request_headers("librarian", "librarian-test-password"),
        )
        self.assertEqual(lib_cases.status_code, 200)
        self.assertIn("借阅证遗失补办", lib_cases.text)
        self.assertIn(f"/dashboard/librarian/cases/{case.case_id}/close", lib_cases.text)
        self.assertNotIn("openid-secret-case", lib_cases.text)
        self.assertNotIn("Reader 50", lib_cases.text)

        lib_fb = self.client.request(
            "GET",
            "/dashboard/librarian?view=feedback",
            headers=self._request_headers("librarian", "librarian-test-password"),
        )
        self.assertEqual(lib_fb.status_code, 200)
        self.assertIn("建议三楼增加电源插座", lib_fb.text)
        self.assertNotIn("openid-secret-fb", lib_fb.text)

        base_headers = {
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://dashboard.example",
            "sec-fetch-site": "same-origin",
        }
        cross_origin = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/close",
            body=urlencode({"confirm": "yes", "resolution_note": "已回复"}).encode(),
            headers={
                **base_headers,
                "origin": "https://evil.example",
                **self._request_headers("librarian", "librarian-test-password"),
            },
        )
        self.assertEqual(cross_origin.status_code, 403)

        missing_confirm = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/close",
            body=urlencode({"resolution_note": "未勾选确认", "view": "cases"}).encode(),
            headers={**base_headers, **self._request_headers("librarian", "librarian-test-password")},
        )
        self.assertEqual(missing_confirm.status_code, 303)
        self.assertIn("result=invalid", missing_confirm.headers["location"])
        self.assertEqual(len(self.store.list_staff_cases(status="open")), 1)

        closed = self.client.request(
            "POST",
            f"/dashboard/librarian/cases/{case.case_id}/close",
            body=urlencode({"confirm": "yes", "resolution_note": "已在后台指引 <挂失>", "view": "cases"}).encode(),
            headers={**base_headers, **self._request_headers("librarian", "librarian-test-password")},
        )
        self.assertEqual(closed.status_code, 303)
        self.assertEqual(closed.headers["location"], "/dashboard/librarian?view=cases&result=closed")
        self.assertEqual(len(self.store.list_staff_cases(status="open")), 0)

        after = self.client.request(
            "GET",
            "/dashboard/librarian?view=cases&result=closed",
            headers=self._request_headers("librarian", "librarian-test-password"),
        )
        self.assertEqual(after.status_code, 200)
        self.assertIn("工单已标记结案", after.text)
        self.assertIn("已在后台指引 &lt;挂失&gt;", after.text)
        self.assertNotIn("<挂失>", after.text)


if __name__ == "__main__":
    unittest.main()
