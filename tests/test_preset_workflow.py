"""Unit tests for Plan A dynamic preset catalog, hot reload, AI draft generation, and dual-review gate."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from support_backend.db import SupportStore
from support_backend.filters import classify_message
from support_backend.preset_workflow import (
    AI_UNAVAILABLE_REASON,
    FALLBACK_GENERATION_NOTE,
    OpenAICompatiblePresetLLM,
    audit_preset_for_publish,
    compute_preset_diff,
    deterministic_preset_guardrails,
    generate_preset_proposal,
)
from support_backend.presets import (
    PresetCatalog,
    PresetDocument,
    export_dynamic_presets,
    render_preset_markdown,
    safe_preset_filename,
    split_frontmatter,
    write_dynamic_preset,
)
from support_backend.responder import SupportResponder
from support_backend.schema import FlushOutcome


class DynamicPresetCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.static_dir = self.root / "static_corpus"
        self.dynamic_dir = self.root / "dynamic_presets"
        self.static_dir.mkdir(parents=True, exist_ok=True)

        (self.static_dir / "1. 图书续借.md").write_text(
            render_preset_markdown(
                preset_id="preset_book_renewal",
                title="图书续借",
                aliases=["续借", "图书续借"],
                applicability="仅适用于读者咨询常规图书续借流程与规则，不适用于续借报错排障。",
                body="每本书可续借一次，续期 15 天。",
            ),
            encoding="utf-8",
        )
        (self.static_dir / "1. 开馆时间.md").write_text(
            render_preset_markdown(
                preset_id="preset_opening_hours",
                title="开馆时间",
                aliases=["开馆时间", "闭馆时间"],
                applicability="仅适用于查询常规开闭馆时间，不适用于节假日临时通知之外的人工投诉。",
                body="日常每日 8:00 至 22:00 开放。",
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_from_corpus_dirs_static_plus_dynamic_override_and_collision_guard(self) -> None:
        # Static-only baseline keeps is_dynamic=False.
        base = PresetCatalog.from_corpus_dir(self.static_dir)
        self.assertEqual(len(base.presets), 2)
        self.assertFalse(base.get_by_id("preset_book_renewal").is_dynamic)

        # Write a dynamic override for preset_book_renewal + a brand-new dynamic preset.
        write_dynamic_preset(
            self.dynamic_dir,
            preset_id="preset_book_renewal",
            title="图书续借（新版）",
            aliases=["续借", "图书续借", "续借怎么操作"],
            applicability="仅适用于读者咨询常规图书续借流程、续借次数与续期规则。",
            body="网页或服务号均可办理续借，每本书限续借一次，续期 15 天。",
        )
        write_dynamic_preset(
            self.dynamic_dir,
            preset_id="preset_study_room",
            title="研讨室预约",
            aliases=["研讨室预约", "怎么约研讨室"],
            applicability="仅适用于读者咨询校内研讨室预约入口与基本规则，不适用于现场冲突处理。",
            body="请通过图书馆空间预约系统提前预约研讨室，按时签到使用。",
        )

        merged = PresetCatalog.from_corpus_dirs(self.static_dir, self.dynamic_dir)
        self.assertEqual(len(merged.presets), 3)

        overridden = merged.get_by_id("preset_book_renewal")
        self.assertIsNotNone(overridden)
        self.assertTrue(overridden.is_dynamic)
        self.assertEqual(overridden.title, "图书续借（新版）")
        self.assertIn("服务号", overridden.body)
        self.assertEqual(merged.match_exact("续借怎么操作？").preset_id, "preset_book_renewal")

        untouched = merged.get_by_id("preset_opening_hours")
        self.assertFalse(untouched.is_dynamic)

        added = merged.get_by_id("preset_study_room")
        self.assertTrue(added.is_dynamic)
        self.assertEqual(merged.match_exact("怎么约研讨室").preset_id, "preset_study_room")

        # Cross-preset alias collision across static + dynamic MUST still raise ValueError.
        write_dynamic_preset(
            self.dynamic_dir,
            preset_id="preset_bad_alias",
            title="重复别名测试",
            aliases=["开馆时间？"],
            applicability="仅用于测试别名冲突检测边界。",
            body="测试正文内容不少于八个字。",
        )
        with self.assertRaises(ValueError):
            PresetCatalog.from_corpus_dirs(self.static_dir, self.dynamic_dir)

    def test_duplicate_preset_id_within_same_layer_raises(self) -> None:
        (self.static_dir / "1. 图书续借副本.md").write_text(
            render_preset_markdown(
                preset_id="preset_book_renewal",
                title="图书续借副本",
                aliases=["续借副本"],
                applicability="用于测试同层 ID 冲突拦截。",
                body="重复 ID 正文内容。",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            PresetCatalog.from_corpus_dirs(self.static_dir, None)

    def test_render_write_rename_cleanup_and_export(self) -> None:
        rendered = render_preset_markdown(
            preset_id="preset_special_chars",
            title='标题含:冒号与"引号"',
            aliases=["  测试别名 ", "", "测试别名", "第二别名"],
            applicability="说明: 含\"引号\"与冒号\n多行兼容测试。",
            body="  正文第一行\n正文第二行  ",
        )
        meta, body = split_frontmatter(rendered)
        self.assertEqual(meta["id"], "preset_special_chars")
        self.assertEqual(meta["title"], '标题含:冒号与"引号"')
        self.assertEqual(meta["aliases"], ["测试别名", "第二别名"])
        self.assertEqual(body, "正文第一行\n正文第二行")

        # Safe filename sanitization & fallback.
        self.assertEqual(safe_preset_filename("../非法/标题\\测试:*?\"<>|\n", "preset_id_1"), "1. 非法 标题 测试.md")
        self.assertEqual(safe_preset_filename("///...:::", "preset_fallback_id"), "1. preset_fallback_id.md")
        long_name = safe_preset_filename("长" * 80, "preset_long")
        self.assertTrue(long_name.startswith("1. "))
        self.assertTrue(long_name.endswith(".md"))
        self.assertLessEqual(len(long_name), len("1. .md") + 60)

        # Write dynamic preset, check permissions, then rename title and verify old file removed.
        first_path = write_dynamic_preset(
            self.dynamic_dir,
            preset_id="preset_wifi",
            title="馆内无线网络",
            aliases=["馆内WiFi", "图书馆WiFi"],
            applicability="仅适用于咨询图书馆无线网络连接方式，不适用于个人设备故障排查。",
            body="请选择校园网 STU-WLAN 并使用校园统一身份认证登录。",
        )
        self.assertTrue(first_path.is_file())
        self.assertEqual(stat.S_IMODE(self.dynamic_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(first_path.stat().st_mode), 0o600)

        renamed_path = write_dynamic_preset(
            self.dynamic_dir,
            preset_id="preset_wifi",
            title="图书馆无线网连接",
            aliases=["馆内WiFi", "连不上馆内无线网怎么连"],
            applicability="仅适用于咨询图书馆无线网络连接方式与账号登录入口。",
            body="连接 STU-WLAN 后打开浏览器输入校园统一身份认证账号密码。",
        )
        self.assertTrue(renamed_path.is_file())
        self.assertFalse(first_path.exists())
        self.assertEqual(len(list(self.dynamic_dir.glob("1.*.md"))), 1)

        # Export into target corpus dir (including cleaning stale filename with same preset_id).
        export_target = self.root / "export_corpus"
        export_target.mkdir(parents=True, exist_ok=True)
        stale_file = export_target / "1. 旧无线网标题.md"
        stale_file.write_text(
            render_preset_markdown(
                preset_id="preset_wifi",
                title="旧无线网标题",
                aliases=["旧别名"],
                applicability="旧版适用条件说明。",
                body="旧版正文内容。",
            ),
            encoding="utf-8",
        )

        exported = export_dynamic_presets(self.dynamic_dir, export_target)
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].name, renamed_path.name)
        self.assertFalse(stale_file.exists())
        self.assertTrue((export_target / renamed_path.name).is_file())


class GuardrailsAndWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = PresetCatalog.from_corpus_dir(Path("rag-poc/corpus"))

    def test_deterministic_guardrails_pii_oneoff_alias_and_allowed_public_info(self) -> None:
        # Clean generalized preset with official library URL and office phone numbers MUST pass.
        clean_reasons = deterministic_preset_guardrails(
            mode="create",
            target_preset_id="",
            preset_id="preset_vpn_access",
            title="校外访问电子资源",
            aliases=["校外访问数据库", "怎么校外下论文"],
            applicability="仅适用于咨询校外访问图书馆电子资源的常规方式，不适用于个人账号锁定排查。",
            body=(
                "请访问图书馆主页 https://www.lib.stu.edu.cn/article-20260101.html 查看 WebVPN 入口。\n"
                "如需人工咨询，可致电咨询台 0754-86503247 或 0754-8650 2958（内线 86503247）。"
            ),
            catalog=self.catalog,
        )
        self.assertEqual(clean_reasons, [])

        # Merge mode reusing own alias MUST pass.
        merge_ok = deterministic_preset_guardrails(
            mode="merge",
            target_preset_id="preset_book_renewal",
            preset_id="preset_book_renewal",
            title="图书续借",
            aliases=["续借", "图书续借", "手机续借图书"],
            applicability="仅适用于咨询常规图书续借流程与规则，不适用于续借报错或逾期解封。",
            body="可通过图书馆主页 https://www.lib.stu.edu.cn/ 或服务号办理续借，每册可续借一次（15天）。",
            catalog=self.catalog,
        )
        self.assertEqual(merge_ok, [])

        # Phone, email, 8+ digit student ID / barcode, one-off phrase, 1-char alias, alias collision.
        bad_reasons = deterministic_preset_guardrails(
            mode="create",
            target_preset_id="",
            preset_id="preset_bad_pii",
            title="临时处理",
            aliases=["书", "图书续借"],
            applicability="适用所有问题。",
            body=(
                "已帮您手动延期，学号 2023102488，条码 987654321，"
                "联系电话 13812345678，邮箱 reader@stu.edu.cn。"
            ),
            catalog=self.catalog,
        )
        joined = "\n".join(bad_reasons)
        self.assertIn("适用条件", joined)
        self.assertIn("单字", joined)
        self.assertIn("冲突", joined)
        self.assertIn("手机号", joined)
        self.assertIn("邮箱", joined)
        self.assertIn("学号", joined)
        self.assertIn("已帮您", joined)

        # Merge mode with mismatched preset_id or non-existent target.
        bad_merge = deterministic_preset_guardrails(
            mode="merge",
            target_preset_id="preset_book_renewal",
            preset_id="preset_other_id",
            title="图书续借",
            aliases=["续借测试"],
            applicability="仅适用于咨询常规图书续借规则与操作步骤。",
            body="通用续借说明正文内容不少于八个字。",
            catalog=self.catalog,
        )
        self.assertTrue(any("一致" in r for r in bad_merge))

        # Create mode with already-existing preset_id.
        dup_create = deterministic_preset_guardrails(
            mode="create",
            target_preset_id="",
            preset_id="preset_book_renewal",
            title="重复续借",
            aliases=["续借重复测试"],
            applicability="仅适用于咨询常规图书续借规则与操作步骤。",
            body="通用续借说明正文内容不少于八个字。",
            catalog=self.catalog,
        )
        self.assertTrue(any("已存在" in r for r in dup_create))

    def test_generate_preset_proposal_create_merge_and_unconfigured_fallback(self) -> None:
        # 1. Create mode via mocked llm_fn.
        seen_prompts: list[tuple[str, str]] = []

        def fake_create_llm(sys_p: str, usr_p: str) -> dict[str, object]:
            seen_prompts.append((sys_p, usr_p))
            return {
                "mode": "create",
                "target_preset_id": "",
                "preset_id": "preset_interlibrary_loan",
                "title": "馆际互借与文献传递",
                "aliases": ["馆际互借", "文献传递怎么申请"],
                "applicability": "仅适用于咨询馆际互借与文献传递申请流程，不适用于个人订单费用核查。",
                "body": "本馆未收藏的文献可通过馆际互借与文献传递系统在线提交申请。",
                "note": "已去除读者个人订单号并转为通用申请指南。",
            }

        proposal_create = generate_preset_proposal(
            question="请问我学号2023000101怎么申请馆际互借？",
            staff_reply="已帮您查了订单，后续请直接登录馆际互借与文献传递系统提交申请。",
            catalog=self.catalog,
            llm_fn=fake_create_llm,
        )
        self.assertEqual(proposal_create.mode, "create")
        self.assertEqual(proposal_create.target_preset_id, "")
        self.assertEqual(proposal_create.preset_id, "preset_interlibrary_loan")
        self.assertEqual(proposal_create.aliases, ("馆际互借", "文献传递怎么申请"))
        self.assertIn("preset_book_renewal", seen_prompts[0][1])

        # 2. Merge mode via mocked llm_fn.
        def fake_merge_llm(sys_p: str, usr_p: str) -> dict[str, object]:
            return {
                "mode": "merge",
                "target_preset_id": "preset_book_renewal",
                "preset_id": "preset_book_renewal",
                "title": "图书续借",
                "aliases": ["续借", "图书续借", "微信怎么续借"],
                "applicability": "仅适用于读者咨询常规图书续借流程、续借次数与续期规则，不适用于续借报错排障。",
                "body": "方案一：网页端登录图书馆主页进入读者中心续借。\n方案二：关注服务号绑定后在读者中心办理续借。\n每本书限续借一次，续期 15 天。",
                "note": "与现有「图书续借」属同一业务范畴，已融合改写。",
            }

        proposal_merge = generate_preset_proposal(
            question="微信上能不能直接续借书？",
            staff_reply="可以，在汕头大学图书馆服务号个人中心绑定后进入读者中心点击我的借阅即可续借。",
            catalog=self.catalog,
            llm_fn=fake_merge_llm,
        )
        self.assertEqual(proposal_merge.mode, "merge")
        self.assertEqual(proposal_merge.target_preset_id, "preset_book_renewal")
        self.assertEqual(proposal_merge.preset_id, "preset_book_renewal")
        self.assertIn("微信怎么续借", proposal_merge.aliases)

        # 3. Unconfigured LLM -> safe fallback draft.
        with patch.dict(os.environ, {"RAG_LLM_API_BASE": "", "RAG_LLM_API_KEY": "", "RAG_LLM_MODEL": ""}, clear=False):
            fallback = generate_preset_proposal(
                question="静音舱怎么预约？",
                staff_reply="请通过空间预约系统提前预约静音舱并按时签到。",
                catalog=self.catalog,
                llm_fn=None,
            )
        self.assertEqual(fallback.mode, "create")
        self.assertEqual(fallback.target_preset_id, "")
        self.assertTrue(fallback.preset_id.startswith("preset_draft_"))
        self.assertEqual(fallback.note, FALLBACK_GENERATION_NOTE)
        self.assertIn("空间预约系统", fallback.body)

    def test_audit_preset_for_publish_all_gates(self) -> None:
        # 1. Deterministic PII rejection short-circuits before AI call.
        ai_called = False

        def should_not_call(sys_p: str, usr_p: str) -> dict[str, object]:
            nonlocal ai_called
            ai_called = True
            return {"passed": True, "reasons": [], "suggestion": ""}

        det_res = audit_preset_for_publish(
            mode="create",
            target_preset_id="",
            preset_id="preset_card_loss",
            title="借阅证挂失",
            aliases=["借阅证挂失"],
            applicability="仅适用于咨询借阅证挂失流程与解挂规则。",
            body="已为您挂失，联系电话 13900001111。",
            catalog=self.catalog,
            llm_fn=should_not_call,
        )
        self.assertFalse(det_res.passed)
        self.assertEqual(det_res.checker, "deterministic")
        self.assertFalse(ai_called)
        self.assertTrue(any("手机号" in r for r in det_res.reasons))

        # 2. AI rejects same-category overlap and recommends merge.
        overlap_res = audit_preset_for_publish(
            mode="create",
            target_preset_id="",
            preset_id="preset_wechat_renewal",
            title="微信图书续借",
            aliases=["微信续借图书"],
            applicability="仅适用于咨询通过微信公众号办理图书续借的流程。",
            body="进入汕头大学图书馆服务号个人中心绑定后，在读者中心我的借阅中点击续借。",
            catalog=self.catalog,
            llm_fn=lambda s, u: {
                "passed": False,
                "reasons": ["该内容与已有模板「图书续借」（preset_book_renewal）属于同一业务范畴，不应新建重复模板。"],
                "suggestion": "请切换为 merge 模式并入 preset_book_renewal 并融合改写。",
                "suggested_mode": "merge",
                "suggested_target_id": "preset_book_renewal",
            },
        )
        self.assertFalse(overlap_res.passed)
        self.assertEqual(overlap_res.checker, "deterministic+ai")
        self.assertEqual(overlap_res.suggested_mode, "merge")
        self.assertEqual(overlap_res.suggested_target_id, "preset_book_renewal")

        # 3. Fail-closed when AI unavailable (unconfigured env or runtime exception).
        with patch.dict(os.environ, {"RAG_LLM_API_BASE": "", "RAG_LLM_API_KEY": "", "RAG_LLM_MODEL": ""}, clear=False):
            unavail_res = audit_preset_for_publish(
                mode="create",
                target_preset_id="",
                preset_id="preset_clean_new",
                title="文献传递服务",
                aliases=["文献传递"],
                applicability="仅适用于读者咨询文献传递常规申请方式，不适用于个人订单费用查询。",
                body="请登录图书馆文献传递平台提交申请，一般 1-3 个工作日反馈结果。",
                catalog=self.catalog,
                llm_fn=None,
            )
        self.assertFalse(unavail_res.passed)
        self.assertEqual(unavail_res.checker, "ai_unavailable")
        self.assertIn(AI_UNAVAILABLE_REASON, unavail_res.reasons)

        def raising_llm(s: str, u: str) -> dict[str, object]:
            raise RuntimeError("upstream 502")

        err_res = audit_preset_for_publish(
            mode="create",
            target_preset_id="",
            preset_id="preset_clean_new",
            title="文献传递服务",
            aliases=["文献传递"],
            applicability="仅适用于读者咨询文献传递常规申请方式，不适用于个人订单费用查询。",
            body="请登录图书馆文献传递平台提交申请，一般 1-3 个工作日反馈结果。",
            catalog=self.catalog,
            llm_fn=raising_llm,
        )
        self.assertFalse(err_res.passed)
        self.assertEqual(err_res.checker, "ai_unavailable")

        # 4. Clean generalized draft passes both deterministic + AI review.
        pass_res = audit_preset_for_publish(
            mode="create",
            target_preset_id="",
            preset_id="preset_clean_new",
            title="文献传递服务",
            aliases=["文献传递", "怎么申请文献传递"],
            applicability="仅适用于读者咨询文献传递常规申请方式，不适用于个人订单费用查询。",
            body="请登录图书馆主页 https://www.lib.stu.edu.cn/ 进入文献传递平台提交申请，咨询电话 0754-86503247。",
            catalog=self.catalog,
            llm_fn=lambda s, u: {
                "passed": True,
                "reasons": [],
                "suggestion": "内容通用、边界清晰，可发布。",
                "suggested_mode": "",
                "suggested_target_id": "",
            },
        )
        self.assertTrue(pass_res.passed)
        self.assertEqual(pass_res.reasons, ())
        self.assertEqual(pass_res.checker, "deterministic+ai")

    def test_openai_compatible_llm_loopback_parses_fenced_json(self) -> None:
        received: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                received.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                resp = {
                    "choices": [
                        {
                            "message": {
                                "content": '```json\n{"passed": true, "reasons": [], "suggestion": "ok"}\n```'
                            }
                        }
                    ]
                }
                encoded = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenAICompatiblePresetLLM(
                api_base=f"http://127.0.0.1:{port}/v1",
                api_key="sk-test-123",
                model="test-chat-model",
                timeout_seconds=5.0,
            )
            self.assertTrue(client.is_configured)
            parsed = client.complete_json("sys prompt", "user prompt")
            self.assertTrue(parsed["passed"])
            self.assertEqual(received[0]["path"], "/v1/chat/completions")
            self.assertEqual(received[0]["auth"], "Bearer sk-test-123")
            self.assertEqual(received[0]["body"]["temperature"], 0.1)
        finally:
            server.shutdown()
            server.server_close()

    def test_compute_preset_diff(self) -> None:
        old_body = "第一行不变\n旧第二行\n第三行不变"
        new_body = "第一行不变\n新第二行\n第三行不变\n新增第四行"
        diff = compute_preset_diff(old_body, new_body)
        self.assertEqual(
            diff,
            [
                {"kind": "same", "text": "第一行不变"},
                {"kind": "del", "text": "旧第二行"},
                {"kind": "add", "text": "新第二行"},
                {"kind": "same", "text": "第三行不变"},
                {"kind": "add", "text": "新增第四行"},
            ],
        )


class ResponderHotReloadTests(unittest.TestCase):
    def test_responder_reload_presets_picks_up_new_and_overridden_dynamic_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            static_dir = root / "static"
            dynamic_dir = root / "dynamic"
            static_dir.mkdir(parents=True, exist_ok=True)
            (static_dir / "1. 图书续借.md").write_text(
                render_preset_markdown(
                    preset_id="preset_book_renewal",
                    title="图书续借",
                    aliases=["图书续借"],
                    applicability="仅适用于常规图书续借流程咨询。",
                    body="旧版续借正文：每本书可续借一次。",
                ),
                encoding="utf-8",
            )

            store = SupportStore(root / "hot_reload.db", merge_enabled=False)
            try:
                catalog = PresetCatalog.from_corpus_dirs(static_dir, dynamic_dir)
                responder = SupportResponder(
                    store=store,
                    preset_catalog=catalog,
                    corpus_dir=static_dir,
                    dynamic_presets_dir=dynamic_dir,
                )
                self.assertEqual(len(responder.preset_catalog.presets), 1)
                self.assertIsNone(responder.preset_catalog.match_exact("怎么连馆内WiFi"))

                # Publish a new dynamic preset and an override for an existing static preset.
                write_dynamic_preset(
                    dynamic_dir,
                    preset_id="preset_book_renewal",
                    title="图书续借",
                    aliases=["图书续借", "线上续借"],
                    applicability="仅适用于常规图书续借流程咨询。",
                    body="新版续借正文：网页或服务号均可续借一次（15天）。",
                )
                write_dynamic_preset(
                    dynamic_dir,
                    preset_id="preset_wifi",
                    title="馆内无线网",
                    aliases=["怎么连馆内WiFi"],
                    applicability="仅适用于咨询图书馆无线网接入方式。",
                    body="请连接 STU-WLAN 并使用统一身份认证登录。",
                )

                count = responder.reload_presets()
                self.assertEqual(count, 2)
                self.assertEqual(responder.preset_catalog.get_by_id("preset_book_renewal").is_dynamic, True)
                self.assertIn("新版续借正文", responder.preset_catalog.get_by_id("preset_book_renewal").body)

                # Verify live triage immediately hits the newly hot-reloaded exact aliases!
                payload_new = {
                    "to_user": "gh_lib",
                    "from_user": "o_hot",
                    "msg_type": "text",
                    "msg_id": "m_hot_1",
                    "content": "怎么连馆内WiFi？",
                }
                res = store.ingest_event(payload_new, classify_message("text", payload_new["content"]))
                outcome = FlushOutcome(
                    batch=res.batch,
                    event_seqs=(res.seq,),
                    route_stage="ready_for_triage",
                )
                plan = asyncio.run(responder.triage(outcome))
                self.assertEqual(plan.stage_path, "exact_alias")
                self.assertEqual(plan.preset_id, "preset_wifi")
                self.assertIn("STU-WLAN", plan.reply_text)

                payload_merged = {
                    "to_user": "gh_lib",
                    "from_user": "o_hot2",
                    "msg_type": "text",
                    "msg_id": "m_hot_2",
                    "content": "线上续借",
                }
                res2 = store.ingest_event(payload_merged, classify_message("text", payload_merged["content"]))
                outcome2 = FlushOutcome(
                    batch=res2.batch,
                    event_seqs=(res2.seq,),
                    route_stage="ready_for_triage",
                )
                plan2 = asyncio.run(responder.triage(outcome2))
                self.assertEqual(plan2.stage_path, "exact_alias")
                self.assertEqual(plan2.preset_id, "preset_book_renewal")
                self.assertIn("新版续借正文", plan2.reply_text)
            finally:
                store.close()

    def test_evaluate_and_generate_preset_proposal_skips_short_and_non_reusable_replies(self):
        from support_backend.preset_workflow import evaluate_and_generate_preset_proposal

        catalog = PresetCatalog(())
        # Very short reply skips LLM entirely
        called = [0]

        def counting_llm(_s: str, _u: str) -> dict[str, object]:
            called[0] += 1
            return {"should_create_draft": True, "title": "不应触发"}

        self.assertIsNone(
            evaluate_and_generate_preset_proposal(
                question="帮我看看",
                staff_reply="好的已处理",
                catalog=catalog,
                llm_fn=counting_llm,
            )
        )
        self.assertEqual(called[0], 0)

        # LLM returning should_create_draft=False skips draft creation
        skipped = evaluate_and_generate_preset_proposal(
            question="我的学号借书权限异常",
            staff_reply="已核对您的个人借阅账户，现已为您单独恢复借阅权限。",
            catalog=catalog,
            llm_fn=lambda _s, _u: {
                "should_create_draft": False,
                "skip_reason": "纯个人账户特例处理，无通用公共规则",
            },
        )
        self.assertIsNone(skipped)

        # LLM returning should_create_draft=True produces a normalized proposal
        created = evaluate_and_generate_preset_proposal(
            question="静音舱怎么预约",
            staff_reply="静音舱可以通过图书馆预约小程序提前一天预约，每次最多使用两小时。",
            catalog=catalog,
            llm_fn=lambda _s, _u: {
                "should_create_draft": True,
                "mode": "create",
                "preset_id": "preset_silent_pod",
                "title": "静音舱预约",
                "aliases": ["静音舱", "静音舱预约"],
                "applicability": "适用于读者咨询图书馆静音舱预约方式与时长。",
                "body": "静音舱可通过图书馆预约小程序提前 1 天预约，每次最多使用 2 小时。",
                "note": "通用空间预约规则",
            },
        )
        self.assertIsNotNone(created)
        assert created is not None
        self.assertEqual(created.preset_id, "preset_silent_pod")
        self.assertEqual(created.title, "静音舱预约")

    def test_extract_json_object_handles_think_fences_multiple_braces_newlines_and_trailing_commas(self):
        from support_backend.shared import extract_json_object

        # 1) <think> block containing curly braces + prose + markdown json fence
        raw1 = (
            "<think>先检查示例 {foo: bar} 再整理输出</think>\n"
            "好的，下面是整理结果：\n"
            "```json\n"
            '{"outcome": "unsupported", "note": "第一行\n第二行",}\n'
            "```\n"
            "希望有帮助。"
        )
        parsed1 = extract_json_object(raw1)
        self.assertEqual(parsed1["outcome"], "unsupported")
        self.assertIn("第二行", parsed1["note"])

        # 2) Multiple brace objects in prose -> takes the final valid dict
        raw2 = '示例格式 {"example": 1}，实际返回 {"outcome": "answered", "source_index": 2}'
        parsed2 = extract_json_object(raw2)
        self.assertEqual(parsed2["outcome"], "answered")
        self.assertEqual(parsed2["source_index"], 2)

        # 3) Pure non-JSON text raises ValueError cleanly
        with self.assertRaises(ValueError):
            extract_json_object("资料中未提及静音舱预约规则。")


if __name__ == "__main__":
    unittest.main()
