from __future__ import annotations

from pathlib import Path
import unittest

from support_backend.auth import (
    ALL_LIBRARIAN_PERMISSIONS,
    DEFAULT_LIBRARIAN_PERMISSIONS,
    PERMISSION_LABELS,
)
from support_backend.dashboard_pages import (
    render_admin_users_page,
    render_login_page,
    render_presets_review_page,
)
from support_backend.presets import PresetDocument


class LoginPageRenderTests(unittest.TestCase):
    def test_renders_standalone_zero_js_login_page(self) -> None:
        html_out = render_login_page(
            next_path="/dashboard/admin",
            style_tag="<style>body{margin:0}</style>",
            favicon_uri="data:image/svg+xml,test",
        )
        self.assertTrue(html_out.startswith("<!doctype html>"))
        self.assertIn("馆", html_out)
        self.assertIn("图书馆智能客服", html_out)
        self.assertIn('<form class="login-form" method="post" action="/dashboard/login">', html_out)
        self.assertIn('<input type="hidden" name="next" value="/dashboard/admin">', html_out)
        self.assertIn('name="username" required autocomplete="username"', html_out)
        self.assertIn('type="password" name="password" required autocomplete="current-password"', html_out)
        self.assertIn("登录工作台", html_out)
        self.assertIn("使用馆员独立账号或管理员账号登录 · 会话仅限 HTTPS 传输", html_out)
        self.assertIn('<div class="login-shell">', html_out)
        self.assertIn('class="login-card"', html_out)
        self.assertIn("<style>body{margin:0}</style>", html_out)
        self.assertIn('<link rel="icon" href="data:image/svg+xml,test">', html_out)
        self.assertNotIn("form-notice--error", html_out)
        self.assertNotIn("<script", html_out.lower())

    def test_renders_error_and_escapes_xss_in_next_path_and_error(self) -> None:
        xss_next = '/dashboard/librarian"><script>alert(1)</script>'
        xss_error = '账号或密码错误 <img src=x onerror="alert(2)">'
        html_out = render_login_page(next_path=xss_next, error=xss_error)
        self.assertIn('class="form-notice form-notice--error" role="alert"', html_out)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertNotIn("<img src=x", html_out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_out)
        self.assertIn("&quot;&gt;&lt;script&gt;", html_out)
        self.assertIn("&lt;img src=x onerror=&quot;alert(2)&quot;&gt;", html_out)


class AdminUsersPageRenderTests(unittest.TestCase):
    def test_renders_empty_users_with_create_form_and_default_perms(self) -> None:
        html_out = render_admin_users_page(
            [],
            current_username="admin",
            notice="账号创建成功",
            notice_kind="info",
        )
        self.assertIn('<p class="form-notice" role="status">账号创建成功</p>', html_out)
        self.assertIn('action="/dashboard/admin/users/create"', html_out)
        self.assertIn("新建馆员/管理员账号", html_out)
        self.assertIn("管理员角色默认拥有全部权限", html_out)
        self.assertIn("创建账号", html_out)
        self.assertIn("当前暂无数据库馆员账号（环境变量超级管理员始终可用）。", html_out)
        self.assertIn('class="perm-grid"', html_out)
        self.assertIn('class="perm-chip"', html_out)
        for perm in ALL_LIBRARIAN_PERMISSIONS:
            self.assertIn(f'value="{perm}"', html_out)
            self.assertIn(PERMISSION_LABELS[perm], html_out)
            if perm in DEFAULT_LIBRARIAN_PERMISSIONS:
                self.assertIn(f'name="perm" value="{perm}" checked>', html_out)
            else:
                self.assertIn(f'name="perm" value="{perm}">', html_out)
        self.assertNotIn("<script", html_out.lower())

    def test_renders_populated_users_escapes_xss_and_omits_password_hash(self) -> None:
        users = [
            {
                "user_id": 1,
                "username": "lib_alice",
                "display_name": 'Alice <b>"XSS"</b>',
                "role": "librarian",
                "permissions": ["view_workbench", "reply_reader"],
                "is_active": True,
                "last_login_utc": "2026-11-28T03:00:00Z",
                "password_hash": "pbkdf2_sha256$200000$SECRET_SALT$SECRET_DIGEST",
            },
            {
                "user_id": 2,
                "username": "chief_bob",
                "display_name": "Bob 馆长",
                "role": "admin",
                "permissions": list(ALL_LIBRARIAN_PERMISSIONS),
                "is_active": False,
                "last_login_utc": None,
                "password_hash": "pbkdf2_sha256$200000$ANOTHER$HASH",
            },
        ]
        html_out = render_admin_users_page(
            users,
            current_username="admin<script>x</script>",
            notice="错误：<script>bad()</script>",
            notice_kind="error",
        )
        # Error notice escaped
        self.assertIn('class="form-notice form-notice--error" role="alert"', html_out)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", html_out)
        self.assertNotIn("<script>", html_out)

        # No password hashes ever appear
        self.assertNotIn("SECRET_SALT", html_out)
        self.assertNotIn("SECRET_DIGEST", html_out)
        self.assertNotIn("pbkdf2_sha256", html_out)

        # User 1 details & forms
        self.assertIn("<code>lib_alice</code>", html_out)
        self.assertIn("Alice &lt;b&gt;&quot;XSS&quot;&lt;/b&gt;", html_out)
        self.assertIn('action="/dashboard/admin/users/lib_alice/update"', html_out)
        self.assertIn('action="/dashboard/admin/users/lib_alice/password"', html_out)
        self.assertIn("2026-11-28T03:00:00Z", html_out)
        self.assertIn('<span class="badge good">启用</span>', html_out)

        # User 2 details & inactive/never-logged-in status
        self.assertIn("<code>chief_bob</code>", html_out)
        self.assertIn('<span class="badge ochre">管理员</span>', html_out)
        self.assertIn('<span class="badge muted">已停用</span>', html_out)
        self.assertIn("从未登录", html_out)
        self.assertIn("保存权限与状态", html_out)
        self.assertIn("重置密码", html_out)
        self.assertIn("更新密码并强制重新登录", html_out)


class PresetsReviewPageRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active_presets = [
            PresetDocument(
                preset_id="preset_hours",
                title="开放时间",
                aliases=("开馆时间", "几点开门"),
                applicability="适用于常规开馆闭馆时间查询",
                body="东海岸馆 8:00-22:00\n旧行内容",
                source_path=Path("/tmp/1. 开放时间.md"),
                is_dynamic=False,
            ),
            {
                "preset_id": "preset_wifi",
                "title": "校园无线网",
                "aliases": ["图书馆WiFi", "无线网怎么连"],
                "applicability": "适用于校内 WiFi 连接指引",
                "body": "连接 STU-WIFI 并使用统一身份认证登录。",
                "is_dynamic": True,
            },
        ]
        self.drafts = [
            {
                "draft_id": 10,
                "source_question": "请问图书馆几点开门？周末呢？",
                "staff_reply": "平时和周末都是 8:00-22:00 开放。",
                "mode": "merge",
                "target_preset_id": "preset_hours",
                "preset_id": "preset_hours",
                "title": "开放时间（含周末）",
                "aliases": ["开馆时间", "周末开放时间"],
                "applicability": "适用于工作日与周末常规开馆时间咨询",
                "body": "东海岸馆 8:00-22:00\n周末照常开放 8:00-22:00",
                "ai_generation_note": "与已有开放时间模板同类，建议合并",
                "ai_review_status": "rejected",
                "ai_review_reasons": ["未注明法定节假日另行通知", "包含 <script>bad</script>"],
                "ai_review_suggestion": "补充节假日安排说明并移除异常字符",
                "status": "draft",
                "created_by": "lib_alice",
                "reviewed_by": "",
                "created_at_utc": "2026-11-28T04:00:00Z",
                "published_at_utc": None,
            },
            {
                "draft_id": 11,
                "source_question": "怎么预约研讨室？",
                "staff_reply": "登录预约系统选择时间段即可。",
                "mode": "create",
                "target_preset_id": "",
                "preset_id": "preset_study_room",
                "title": "研讨室预约",
                "aliases": ["研讨室怎么预约", "预约研讨间"],
                "applicability": "适用于普通读者预约图书馆研讨室",
                "body": "请通过图书馆公众号菜单进入研讨室预约系统。",
                "ai_generation_note": "全新主题，建议新建",
                "ai_review_status": "passed",
                "ai_review_reasons": [],
                "ai_review_suggestion": "已通过双审",
                "status": "published",
                "created_by": "lib_alice",
                "reviewed_by": "admin",
                "created_at_utc": "2026-11-27T08:00:00Z",
                "published_at_utc": "2026-11-27T09:00:00Z",
            },
            {
                "draft_id": 12,
                "source_question": "我的学号 2023123456 借不了书",
                "staff_reply": "已帮您重置借阅权限。",
                "mode": "create",
                "target_preset_id": "",
                "preset_id": "preset_case_12",
                "title": "个案处理",
                "aliases": ["借不了书"],
                "applicability": "个案",
                "body": "已帮您处理学号 2023123456。",
                "ai_generation_note": "",
                "ai_review_status": "rejected",
                "ai_review_reasons": ["包含学号与一次性个案措辞"],
                "ai_review_suggestion": "不应沉淀为标准模板",
                "status": "rejected",
                "created_by": "lib_bob",
                "reviewed_by": "admin",
                "created_at_utc": "2026-11-26T10:00:00Z",
                "published_at_utc": None,
            },
        ]

    def test_draft_tab_renders_merge_diff_audit_box_and_reviewer_buttons(self) -> None:
        html_out = render_presets_review_page(
            self.drafts,
            self.active_presets,
            active_tab="draft",
            can_review=True,
            notice="已保存修改",
        )
        # Tabs & active state
        self.assertIn('href="/dashboard/presets?tab=draft" aria-current="page"', html_out)
        self.assertIn('href="/dashboard/presets?tab=published"', html_out)
        self.assertIn('href="/dashboard/presets?tab=rejected"', html_out)
        self.assertIn('href="/dashboard/presets?tab=active"', html_out)
        self.assertIn("当前生效 1. 模板", html_out)

        # Only draft #10 rendered in draft tab
        self.assertIn("#10", html_out)
        self.assertNotIn("#11", html_out)
        self.assertNotIn("#12", html_out)

        # Badges & Audit box
        self.assertIn('<span class="badge ochre">并入已有模板</span>', html_out)
        self.assertIn('<span class="badge urgent">AI 驳回</span>', html_out)
        self.assertIn('class="audit-box audit-box--rejected"', html_out)
        self.assertIn("未注明法定节假日另行通知", html_out)
        self.assertIn("补充节假日安排说明并移除异常字符", html_out)
        self.assertNotIn("<script>bad</script>", html_out)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", html_out)

        # Merge diff preview against live preset_hours
        self.assertIn('class="diff-box"', html_out)
        self.assertIn('class="diff-line diff-line--same">  东海岸馆 8:00-22:00</div>', html_out)
        self.assertIn('class="diff-line diff-line--del">- 旧行内容</div>', html_out)
        self.assertIn('class="diff-line diff-line--add">+ 周末照常开放 8:00-22:00</div>', html_out)

        # Editable form & reviewer buttons
        self.assertIn('action="/dashboard/presets/drafts/10/save"', html_out)
        self.assertIn('name="intent" value="save">保存修改</button>', html_out)
        self.assertIn('name="intent" value="publish">AI 终审并发布生效</button>', html_out)
        self.assertIn('name="intent" value="reject">驳回草稿</button>', html_out)
        self.assertNotIn("<script", html_out.lower())

    def test_draft_tab_hides_publish_and_reject_when_can_review_false(self) -> None:
        html_out = render_presets_review_page(
            self.drafts,
            self.active_presets,
            active_tab="draft",
            can_review=False,
        )
        self.assertIn('name="intent" value="save">保存修改</button>', html_out)
        self.assertNotIn('value="publish"', html_out)
        self.assertNotIn('value="reject"', html_out)

    def test_published_and_rejected_tabs_render_readonly(self) -> None:
        pub_html = render_presets_review_page(
            self.drafts,
            self.active_presets,
            active_tab="published",
            can_review=True,
        )
        self.assertIn('href="/dashboard/presets?tab=published" aria-current="page"', pub_html)
        self.assertIn("#11", pub_html)
        self.assertIn("研讨室预约", pub_html)
        self.assertIn("<code>preset_study_room</code>", pub_html)
        self.assertIn("2026-11-27T09:00:00Z", pub_html)
        self.assertIn("audit-box audit-box--passed", pub_html)
        self.assertNotIn('class="draft-edit-form"', pub_html)

        rej_html = render_presets_review_page(
            self.drafts,
            self.active_presets,
            active_tab="rejected",
            can_review=True,
        )
        self.assertIn('href="/dashboard/presets?tab=rejected" aria-current="page"', rej_html)
        self.assertIn("#12", rej_html)
        self.assertIn("包含学号与一次性个案措辞", rej_html)
        self.assertNotIn('class="draft-edit-form"', rej_html)

    def test_active_tab_handles_dataclass_and_dict_and_escapes_xss(self) -> None:
        xss_presets = [
            *self.active_presets,
            {
                "preset_id": 'preset_"xss"',
                "title": "<script>evil()</script>",
                "aliases": ['<img src=x onerror=1>'],
                "applicability": "适用 <b>all</b>",
                "body": "正文 <script>alert(3)</script>",
                "is_dynamic": True,
            },
        ]
        html_out = render_presets_review_page(
            [],
            xss_presets,
            active_tab="active",
        )
        self.assertIn('href="/dashboard/presets?tab=active" aria-current="page"', html_out)
        self.assertIn('<span class="badge muted">仓库基线</span>', html_out)
        self.assertIn('<span class="badge good">动态发布</span>', html_out)
        self.assertIn("开放时间", html_out)
        self.assertIn("校园无线网", html_out)
        self.assertIn('<pre class="preset-body-pre">', html_out)
        self.assertNotIn("<script>evil()</script>", html_out)
        self.assertNotIn("<script>alert(3)</script>", html_out)
        self.assertNotIn("<img src=x", html_out)
        self.assertIn("&lt;script&gt;evil()&lt;/script&gt;", html_out)
        self.assertIn("&lt;img src=x onerror=1&gt;", html_out)

    def test_empty_state_for_draft_tab(self) -> None:
        html_out = render_presets_review_page([], [], active_tab="draft")
        self.assertIn("当前没有待审核的模板草稿。", html_out)


if __name__ == "__main__":
    unittest.main()
