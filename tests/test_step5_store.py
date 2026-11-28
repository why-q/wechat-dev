"""Unit tests for Step 5 storage: Schema v5 migration, Auth/RBAC, Reply Quota, and Preset Drafts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support_backend.auth import (
    ALL_LIBRARIAN_PERMISSIONS,
    DEFAULT_LIBRARIAN_PERMISSIONS,
    PERM_CLOSE_CASE,
    PERM_DRAFT_PRESET,
    PERM_REPLY_READER,
    PERM_REVIEW_PRESET,
    PERM_VIEW_WORKBENCH,
    PERMISSION_LABELS,
    DashboardPrincipal,
    generate_session_token,
    hash_password,
    hash_session_token,
    normalize_permissions,
    valid_username,
    verify_password,
)
from support_backend.db import (
    WECHAT_INTERACTION_QUOTA_LIMIT,
    WECHAT_INTERACTION_WINDOW_SECONDS,
    SupportStore,
)
from support_backend.filters import classify_message
from support_backend.schema import SCHEMA_VERSION

_UTC8 = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class AuthHelperTests(unittest.TestCase):
    def test_password_hashing_and_verification(self):
        hashed = hash_password("LibSecret123", iterations=10_000)
        self.assertTrue(hashed.startswith("pbkdf2_sha256$10000$"))
        self.assertEqual(len(hashed.split("$")), 4)
        self.assertTrue(verify_password("LibSecret123", hashed))
        self.assertFalse(verify_password("WrongPass", hashed))
        # Different salts produce different hashes.
        hashed2 = hash_password("LibSecret123", iterations=10_000)
        self.assertNotEqual(hashed, hashed2)
        self.assertTrue(verify_password("LibSecret123", hashed2))
        # Malformed inputs fail safely without raising.
        for bad in ("", "plain", "md5$1$a$b", "pbkdf2_sha256$0$aa$bb", "pbkdf2_sha256$abc$aa$bb", "pbkdf2_sha256$10$!!!$@@@"):
            self.assertFalse(verify_password("LibSecret123", bad))

    def test_principal_permissions_and_helpers(self):
        self.assertEqual(set(PERMISSION_LABELS.keys()), set(ALL_LIBRARIAN_PERMISSIONS))
        self.assertEqual(
            normalize_permissions([PERM_REVIEW_PRESET, "unknown", PERM_VIEW_WORKBENCH, PERM_REVIEW_PRESET]),
            (PERM_VIEW_WORKBENCH, PERM_REVIEW_PRESET),
        )
        self.assertTrue(valid_username("lib_01.a-b"))
        self.assertFalse(valid_username("a"))
        self.assertFalse(valid_username("bad user"))
        self.assertFalse(valid_username("x" * 33))

        admin = DashboardPrincipal(
            username="root", display_name="管理员", role="admin", permissions=frozenset(), source="env_admin"
        )
        self.assertTrue(admin.is_admin)
        self.assertTrue(admin.has_permission(PERM_REVIEW_PRESET))

        lib = DashboardPrincipal(
            username="zhang",
            display_name="张馆员",
            role="librarian",
            permissions=frozenset({PERM_VIEW_WORKBENCH, PERM_REPLY_READER}),
            source="db",
        )
        self.assertFalse(lib.is_admin)
        self.assertTrue(lib.has_permission(PERM_REPLY_READER))
        self.assertFalse(lib.has_permission(PERM_REVIEW_PRESET))

        tok = generate_session_token()
        self.assertGreaterEqual(len(tok), 32)
        self.assertEqual(len(hash_session_token(tok)), 64)


class Step5StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "support.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _epoch_utc8(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
        return datetime(year, month, day, hour, minute, tzinfo=_UTC8).timestamp()

    def test_v4_to_v5_schema_migration(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE staff_cases (
                case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, reader_id TEXT NOT NULL,
                source_batch_id INTEGER, source_event_seq INTEGER,
                case_kind TEXT NOT NULL DEFAULT 'staff_todo',
                status TEXT NOT NULL DEFAULT 'open',
                summary TEXT NOT NULL, latest_message TEXT NOT NULL,
                created_at_epoch REAL NOT NULL, created_at_utc TEXT NOT NULL,
                updated_at_epoch REAL NOT NULL, updated_at_utc TEXT NOT NULL,
                closed_at_utc TEXT, resolution_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE staff_case_messages (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                batch_id INTEGER, event_seq INTEGER,
                sender_role TEXT NOT NULL, content TEXT NOT NULL,
                created_at_epoch REAL NOT NULL, created_at_utc TEXT NOT NULL
            );
            CREATE TABLE outbound_replies (
                reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, reader_id TEXT NOT NULL,
                trigger_kind TEXT NOT NULL, trigger_ref_id INTEGER,
                content TEXT NOT NULL, delivery_mode TEXT NOT NULL, status TEXT NOT NULL,
                errcode INTEGER, errmsg TEXT NOT NULL DEFAULT '', duration_ms INTEGER,
                created_at_epoch REAL NOT NULL, created_at_utc TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '4');
            INSERT INTO staff_cases(
                account_id, reader_id, case_kind, status, summary, latest_message,
                created_at_epoch, created_at_utc, updated_at_epoch, updated_at_utc
            ) VALUES ('gh_v4', 'o_v4_reader', 'staff_todo', 'open', '旧版工单', '旧内容',
                      1700000000.0, '2023-11-14T22:13:20+00:00',
                      1700000000.0, '2023-11-14T22:13:20+00:00');
            """
        )
        conn.commit()
        conn.close()

        store = SupportStore(self.db_path)
        try:
            self.assertEqual(SCHEMA_VERSION, 5)
            self.assertEqual(store.overview_metrics()["schema_version"], 5)

            # Verify migrated columns and new tables exist.
            cases = store.list_staff_cases(status="open")
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["closed_by"], "")

            closed = store.close_staff_case(
                cases[0]["case_id"], resolution_note="已解决", closed_by="lib_chen"
            )
            self.assertIsNotNone(closed)
            assert closed is not None
            self.assertEqual(closed.closed_by, "lib_chen")
            self.assertEqual(closed.to_dict()["closed_by"], "lib_chen")

            rid = store.record_outbound_reply(
                account_id="gh_v4",
                reader_id="o_v4_reader",
                trigger_kind="staff_dashboard_reply",
                trigger_ref_id=1,
                content="已为您处理",
                delivery_mode="live",
                status="sent",
                operator_username="lib_chen",
            )
            self.assertGreater(rid, 0)
            replies = store.list_outbound_replies(limit=5)
            self.assertEqual(replies[0]["operator_username"], "lib_chen")

            # New tables work cleanly after migration.
            user = store.create_dashboard_user(username="lib_chen", password="password123")
            self.assertEqual(user["username"], "lib_chen")
            draft = store.create_preset_draft(title="测试模板", preset_id="test_1", created_by="lib_chen")
            self.assertEqual(draft["draft_id"], 1)
        finally:
            store.close()

    def test_dashboard_user_rbac_and_session_lifecycle(self):
        clock = FakeClock(1_800_000_000.0)
        store = SupportStore(self.db_path, clock=clock)
        try:
            # Validation errors.
            with self.assertRaises(ValueError):
                store.create_dashboard_user(username="x", password="password123")
            with self.assertRaises(ValueError):
                store.create_dashboard_user(username="valid_user", password="123")
            with self.assertRaises(ValueError):
                store.create_dashboard_user(username="valid_user", password="password123", role="superuser")

            u = store.create_dashboard_user(
                username="lib_wang",
                password="secret123",
                display_name="王馆员",
                role="librarian",
            )
            self.assertNotIn("password_hash", u)
            self.assertEqual(tuple(u["permissions"]), DEFAULT_LIBRARIAN_PERMISSIONS)
            self.assertTrue(u["is_active"])
            self.assertIsNone(u["last_login_utc"])

            # Duplicate username rejected.
            with self.assertRaises(ValueError):
                store.create_dashboard_user(username="lib_wang", password="another_password")

            # List & get never expose password_hash.
            listed = store.list_dashboard_users()
            self.assertEqual(len(listed), 1)
            self.assertNotIn("password_hash", listed[0])
            fetched = store.get_dashboard_user("lib_wang")
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertNotIn("password_hash", fetched)

            # Verify password & check last_login_utc update.
            self.assertIsNone(store.verify_dashboard_user("lib_wang", "wrongpass"))
            principal = store.verify_dashboard_user("lib_wang", "secret123")
            self.assertIsNotNone(principal)
            assert principal is not None
            self.assertEqual(principal.username, "lib_wang")
            self.assertEqual(principal.source, "db")
            self.assertTrue(principal.has_permission(PERM_REPLY_READER))
            self.assertFalse(principal.has_permission(PERM_REVIEW_PRESET))
            self.assertIsNotNone(store.get_dashboard_user("lib_wang")["last_login_utc"])

            # Session creation, lookup, expiry, and purge.
            tok1 = store.create_dashboard_session("lib_wang", "librarian", ttl_seconds=600)
            tok2 = store.create_dashboard_session("lib_wang", "librarian", ttl_seconds=1200)
            sess1 = store.get_dashboard_session(tok1)
            self.assertIsNotNone(sess1)
            assert sess1 is not None
            self.assertEqual(sess1["username"], "lib_wang")
            self.assertEqual(sess1["role"], "librarian")

            clock.advance(601)
            # tok1 expired and auto-deleted on lookup; tok2 still active.
            self.assertIsNone(store.get_dashboard_session(tok1))
            self.assertIsNotNone(store.get_dashboard_session(tok2))

            # Explicit delete.
            store.delete_dashboard_session(tok2)
            self.assertIsNone(store.get_dashboard_session(tok2))

            # Disabling user invalidates all active sessions & blocks login.
            tok3 = store.create_dashboard_session("lib_wang", "librarian", ttl_seconds=3600)
            updated = store.update_dashboard_user(
                "lib_wang",
                display_name="王老师",
                permissions=[PERM_VIEW_WORKBENCH, PERM_REVIEW_PRESET, "bogus"],
                is_active=False,
            )
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["display_name"], "王老师")
            self.assertEqual(updated["permissions"], [PERM_VIEW_WORKBENCH, PERM_REVIEW_PRESET])
            self.assertFalse(updated["is_active"])
            self.assertIsNone(store.get_dashboard_session(tok3))
            self.assertIsNone(store.verify_dashboard_user("lib_wang", "secret123"))

            # Re-enable and change password -> invalidates active sessions.
            store.update_dashboard_user("lib_wang", is_active=True)
            tok4 = store.create_dashboard_session("lib_wang", "librarian", ttl_seconds=3600)
            with self.assertRaises(ValueError):
                store.set_dashboard_user_password("lib_wang", "short")
            self.assertFalse(store.set_dashboard_user_password("missing_user", "newpass123"))
            self.assertTrue(store.set_dashboard_user_password("lib_wang", "newpass123"))
            self.assertIsNone(store.get_dashboard_session(tok4))
            self.assertIsNone(store.verify_dashboard_user("lib_wang", "secret123"))
            self.assertIsNotNone(store.verify_dashboard_user("lib_wang", "newpass123"))

            # Purge expired sessions.
            tok_short = store.create_dashboard_session("lib_wang", "librarian", ttl_seconds=60)
            tok_long = store.create_dashboard_session("lib_wang", "librarian", ttl_seconds=3600)
            clock.advance(120)
            purged = store.purge_expired_sessions()
            self.assertEqual(purged, 1)
            self.assertIsNone(store.get_dashboard_session(tok_short))
            self.assertIsNotNone(store.get_dashboard_session(tok_long))
        finally:
            store.close()

    def test_reader_reply_quota_window_exhaustion_and_menu_events(self):
        clock = FakeClock(1_800_000_000.0)
        store = SupportStore(self.db_path, clock=clock)
        try:
            # No inbound messages at all.
            q0 = store.get_reader_reply_quota("gh_lib", "o_reader_1")
            self.assertEqual(
                q0,
                {
                    "window_open": False,
                    "window_remaining_seconds": 0,
                    "sent_in_window": 0,
                    "quota_limit": WECHAT_INTERACTION_QUOTA_LIMIT,
                    "quota_remaining": 0,
                    "can_reply": False,
                    "blocked_reason": "no_inbound",
                },
            )
            self.assertNotIn("reader_id", q0)

            # Menu / subscribe events must NOT open the 48h reply window.
            store.ingest_event(
                {
                    "transport": "cloud",
                    "msg_type": "event",
                    "event": "CLICK",
                    "from_user": "o_reader_1",
                    "to_user": "gh_lib",
                    "content": "",
                },
                classify_message(msg_type="event", content="", event="CLICK"),
            )
            q_event = store.get_reader_reply_quota("gh_lib", "o_reader_1")
            self.assertFalse(q_event["can_reply"])
            self.assertEqual(q_event["blocked_reason"], "no_inbound")

            # Reader sends a real text message -> opens 48h window with 5/5 quota.
            ing = store.ingest_event(
                {
                    "transport": "cloud",
                    "msg_type": "text",
                    "event": "",
                    "msg_id": "m1",
                    "from_user": "o_reader_1",
                    "to_user": "gh_lib",
                    "content": "请问借书证丢了怎么办",
                },
                classify_message("text", "请问借书证丢了怎么办"),
            )
            q_fresh = store.get_reader_reply_quota("gh_lib", "o_reader_1")
            self.assertTrue(q_fresh["window_open"])
            self.assertEqual(q_fresh["window_remaining_seconds"], WECHAT_INTERACTION_WINDOW_SECONDS)
            self.assertEqual(q_fresh["sent_in_window"], 0)
            self.assertEqual(q_fresh["quota_remaining"], 5)
            self.assertTrue(q_fresh["can_reply"])
            self.assertEqual(q_fresh["blocked_reason"], "")

            # Shadow / failed replies do NOT consume quota; only status='sent' consumes quota.
            store.record_outbound_reply(
                account_id="gh_lib",
                reader_id="o_reader_1",
                trigger_kind="staff_dashboard_reply",
                trigger_ref_id=ing.seq,
                content="shadow reply",
                delivery_mode="shadow",
                status="shadow",
                operator_username="lib_a",
            )
            store.record_outbound_reply(
                account_id="gh_lib",
                reader_id="o_reader_1",
                trigger_kind="staff_dashboard_reply",
                trigger_ref_id=ing.seq,
                content="failed reply",
                delivery_mode="live",
                status="failed",
                operator_username="lib_a",
            )
            self.assertEqual(store.get_reader_reply_quota("gh_lib", "o_reader_1")["sent_in_window"], 0)

            # Send 5 successful replies -> exhausts 5/5 quota.
            for idx in range(5):
                clock.advance(10)
                store.record_outbound_reply(
                    account_id="gh_lib",
                    reader_id="o_reader_1",
                    trigger_kind="staff_dashboard_reply",
                    trigger_ref_id=ing.seq,
                    content=f"回复{idx + 1}",
                    delivery_mode="live",
                    status="sent",
                    operator_username="lib_a",
                )
            q_exhausted = store.get_reader_reply_quota("gh_lib", "o_reader_1")
            self.assertTrue(q_exhausted["window_open"])
            self.assertEqual(q_exhausted["sent_in_window"], 5)
            self.assertEqual(q_exhausted["quota_remaining"], 0)
            self.assertFalse(q_exhausted["can_reply"])
            self.assertEqual(q_exhausted["blocked_reason"], "quota_exhausted")

            # Verify inbox projects reply_quota & staff_replies without leaking reader_id.
            inbox = store.list_librarian_inbox(limit=10)
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["reply_quota"]["blocked_reason"], "quota_exhausted")
            self.assertEqual(len(inbox[0]["staff_replies"]), 7)
            self.assertEqual(inbox[0]["staff_replies"][-1]["operator_username"], "lib_a")
            self.assertNotIn("reader_id", json.dumps(inbox, ensure_ascii=False))
            self.assertNotIn("o_reader_1", json.dumps(inbox, ensure_ascii=False))

            # Advance past 48h -> window_expired.
            clock.advance(WECHAT_INTERACTION_WINDOW_SECONDS)
            q_expired = store.get_reader_reply_quota("gh_lib", "o_reader_1")
            self.assertFalse(q_expired["window_open"])
            self.assertEqual(q_expired["window_remaining_seconds"], 0)
            self.assertFalse(q_expired["can_reply"])
            self.assertEqual(q_expired["blocked_reason"], "window_expired")

            # Target lookup helpers.
            ev_target = store.get_inbox_event_target(ing.seq)
            self.assertIsNotNone(ev_target)
            assert ev_target is not None
            self.assertEqual(ev_target["reader_id"], "o_reader_1")
            # Menu event seq must return None from get_inbox_event_target.
            self.assertIsNone(store.get_inbox_event_target(1))
        finally:
            store.close()

    def test_append_case_reply_open_and_closed_and_analytics_privacy(self):
        now_ep = self._epoch_utc8(2026, 11, 28, 15, 0)
        clock = FakeClock(now_ep)
        store = SupportStore(self.db_path, clock=clock)
        try:
            # Ingest reader messages so 48h window is open for both readers.
            store.ingest_event(
                {
                    "transport": "cloud",
                    "msg_type": "text",
                    "event": "",
                    "msg_id": "m_open",
                    "from_user": "o_secret_open",
                    "to_user": "gh_lib",
                    "content": "自习室门禁卡打不开",
                },
                classify_message("text", "自习室门禁卡打不开"),
                received_at_epoch=now_ep - 600,
            )
            store.ingest_event(
                {
                    "transport": "cloud",
                    "msg_type": "text",
                    "event": "",
                    "msg_id": "m_closed",
                    "from_user": "o_secret_closed",
                    "to_user": "gh_lib",
                    "content": "查收电子资源权限",
                },
                classify_message("text", "查收电子资源权限"),
                received_at_epoch=now_ep - 1800,
            )

            c_open = store.create_staff_case(
                account_id="gh_lib",
                reader_id="o_secret_open",
                summary="门禁问题",
                latest_message="自习室门禁卡打不开",
                now_epoch=now_ep - 600,
            )
            c_closed = store.create_staff_case(
                account_id="gh_lib",
                reader_id="o_secret_closed",
                summary="电子资源权限",
                latest_message="查收电子资源权限",
                now_epoch=now_ep - 1800,
            )
            store.close_staff_case(
                c_closed.case_id,
                resolution_note="已核对开通",
                closed_by="lib_li",
                now_epoch=now_ep - 1200,
            )

            # Append replies to BOTH open and closed cases.
            self.assertTrue(
                store.append_staff_case_reply(
                    c_open.case_id, "请到一楼总台刷新校园卡", operator_username="lib_zhang", now_epoch=now_ep - 300
                )
            )
            self.assertTrue(
                store.append_staff_case_reply(
                    c_closed.case_id, "补充说明：VPN 登录后即可访问", operator_username="lib_li", now_epoch=now_ep - 60
                )
            )
            self.assertFalse(store.append_staff_case_reply(999999, "不存在工单"))
            self.assertFalse(store.append_staff_case_reply(c_open.case_id, "   "))

            # Verify get_staff_case_target returns reader_id for backend sending.
            target = store.get_staff_case_target(c_closed.case_id)
            self.assertIsNotNone(target)
            assert target is not None
            self.assertEqual(target["reader_id"], "o_secret_closed")
            self.assertEqual(target["status"], "closed")

            # Default include_closed=False returns empty closed_cases.
            a_default = store.dashboard_librarian_analytics(now_epoch=now_ep)
            self.assertEqual(a_default["closed_cases"], [])
            self.assertEqual(len(a_default["open_todo"]), 1)
            self.assertTrue(a_default["open_todo"][0]["reply_quota"]["can_reply"])
            self.assertEqual(a_default["open_todo"][0]["messages"][-1]["operator_username"], "lib_zhang")

            # include_closed=True returns closed_cases with closed_by, messages, and reply_quota.
            a_full = store.dashboard_librarian_analytics(now_epoch=now_ep, include_closed=True)
            self.assertEqual(len(a_full["closed_cases"]), 1)
            cc = a_full["closed_cases"][0]
            self.assertEqual(cc["case_id"], c_closed.case_id)
            self.assertEqual(cc["status"], "closed")
            self.assertEqual(cc["closed_by"], "lib_li")
            self.assertEqual(cc["resolution_note"], "已核对开通")
            self.assertTrue(cc["reply_quota"]["can_reply"])
            self.assertEqual(len(cc["messages"]), 3)
            self.assertEqual(cc["messages"][-1]["content"], "补充说明：VPN 登录后即可访问")
            self.assertEqual(cc["messages"][-1]["operator_username"], "lib_li")

            # Zero reader_id / OpenID leakage in analytics payload!
            dumped = json.dumps(a_full, ensure_ascii=False)
            self.assertNotIn("reader_id", dumped)
            self.assertNotIn("o_secret_", dumped)
        finally:
            store.close()

    def test_preset_drafts_full_lifecycle(self):
        clock = FakeClock(1_800_000_000.0)
        store = SupportStore(self.db_path, clock=clock)
        try:
            with self.assertRaises(ValueError):
                store.create_preset_draft(mode="invalid_mode", title="x")

            d1 = store.create_preset_draft(
                source_case_id=12,
                source_event_seq=34,
                source_question="周末几点闭馆？",
                staff_reply="周末 22:00 闭馆。",
                mode="create",
                preset_id="weekend_hours",
                title="周末开放时间",
                aliases=["周末闭馆", "周六周日开放时间", "周末闭馆"],
                applicability="读者咨询周末开闭馆时间时使用",
                body="图书馆周末开放时间为 08:00-22:00。",
                ai_generation_note="新建常规问答模板",
                created_by="lib_zhang",
            )
            self.assertEqual(d1["status"], "draft")
            self.assertEqual(d1["ai_review_status"], "pending")
            self.assertEqual(d1["aliases"], ["周末闭馆", "周六周日开放时间"])

            # Review passes.
            clock.advance(5)
            reviewed = store.set_preset_draft_review(
                d1["draft_id"],
                ai_review_status="passed",
                reasons=[],
                suggestion="内容规范，可发布",
                reviewed_by="admin",
            )
            self.assertIsNotNone(reviewed)
            assert reviewed is not None
            self.assertEqual(reviewed["ai_review_status"], "passed")
            self.assertEqual(reviewed["ai_review_suggestion"], "内容规范，可发布")

            # Editing content resets ai_review_status back to 'pending'.
            clock.advance(5)
            edited = store.update_preset_draft(
                d1["draft_id"],
                body="图书馆周末开放时间为 08:30-22:00。",
                aliases=["周末闭馆", "双休日开放时间"],
            )
            self.assertIsNotNone(edited)
            assert edited is not None
            self.assertEqual(edited["ai_review_status"], "pending")
            self.assertEqual(edited["ai_review_reasons"], [])
            self.assertEqual(edited["ai_review_suggestion"], "")
            self.assertEqual(edited["body"], "图书馆周末开放时间为 08:30-22:00。")

            # Re-review and publish.
            clock.advance(5)
            store.set_preset_draft_review(
                d1["draft_id"],
                ai_review_status="passed",
                reasons=[],
                suggestion="通过",
                reviewed_by="admin",
            )
            published = store.mark_preset_draft_published(d1["draft_id"], reviewed_by="admin")
            self.assertIsNotNone(published)
            assert published is not None
            self.assertEqual(published["status"], "published")
            self.assertIsNotNone(published["published_at_utc"])
            self.assertEqual(published["reviewed_by"], "admin")

            # Create a second draft and reject it.
            clock.advance(5)
            d2 = store.create_preset_draft(
                mode="merge",
                target_preset_id="weekend_hours",
                preset_id="weekend_hours",
                title="周末开放时间（合并）",
                body="含个人学号 20230001 的回复",
                created_by="lib_li",
            )
            rejected = store.reject_preset_draft(
                d2["draft_id"], reviewed_by="admin", reason="包含个人隐私学号"
            )
            self.assertIsNotNone(rejected)
            assert rejected is not None
            self.assertEqual(rejected["status"], "rejected")
            self.assertIn("包含个人隐私学号", rejected["ai_review_reasons"])

            # Filtering by status.
            self.assertEqual(len(store.list_preset_drafts()), 2)
            self.assertEqual(len(store.list_preset_drafts(status="published")), 1)
            self.assertEqual(len(store.list_preset_drafts(status="rejected")), 1)
            self.assertEqual(store.get_preset_draft(d2["draft_id"])["status"], "rejected")
            self.assertIsNone(store.get_preset_draft(99999))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
