# Step 5：独立馆员权限体系、面板直连回复与 `1.` 模板 AI 双审热重载方案

> 日期：2026-11-28  
> 状态：设计已确认，按 M1 -> M2 -> M3 分期增量落地。

## 1. 业务共识与边界

1. **多账号独立权限（M1）**：
   - 升级网页登录页（`/dashboard/login` + `HttpOnly; Secure; SameSite=Lax` Session Cookie + 登出），兼容 `Authorization: Basic` 供运维/自动化测试调用；保留环境变量 `admin` 作为超级管理员兜底账号。
   - SQLite 新增 `dashboard_users` 与 `dashboard_sessions` 表（PBKDF2-HMAC-SHA256 加盐哈希密码），管理员可增删改馆员账号、启停状态、重置密码与逐项勾选权限位：`view_workbench`、`reply_reader`、`close_case`、`draft_preset`、`review_preset`。
   - 所有回复、结案、草稿生成与审核发布均记录 `operator_username`。

2. **馆员面板直连微信回复（M2）**：
   - 工单全部支持回复（未结与已结工单在 48h 窗口内均可回复）；默认仅展示未结案工单，提供筛选开关切换「仅看未结案 / 显示全部工单（含已结）」。
   - 读者私信流中 48h 内消息亦支持直接补答（便于灰度/影子期巡检补位）。
   - 前端只传 `case_id` / `event_seq`，绝不暴露 OpenID；后端实时计算读者 48h 交互窗口剩余时间与 48h 内 `custom/send` 已用条数（`x/5`），超窗或满额灰显提示改走公众号后台。
   - 馆员主动回复不受自动回复灰度白名单门控；支持「发送回复」与「发送并结案」（仅发信成功才结案，失败红框报错）。

3. **`1.` 标准模板自动转换 + AI/管理员双审 + 方案 A 热重载（M3）**：
   - **存储采用方案 A**：保持仓库 `rag-poc/corpus` 只读挂载不变；动态发布目录设为 `/var/lib/private/wechat-relay/presets/`（环境变量 `SUPPORT_DYNAMIC_PRESETS_DIR`）。`PresetCatalog.from_corpus_dirs(static_dir, dynamic_dir)` 合并加载，同 `preset_id` 时动态目录优先覆盖静态基线，实现「并入已有模板」免改只读仓库；同时提供 CLI 导出同步回 Git 仓库。
   - **AI 生成与同类并入**：馆员回复勾选「生成模板草稿」（或在工单上触发）时，LLM 结合原问题、馆员回复与现有全部 `1.` 模板摘要，判断应 `create`（新建）还是 `merge`（并入同一范畴已有模板并融合改写正文/别名/适用条件，而非粗暴 append），并在审核页展示新旧版本 Diff。
   - **双审门控（AI 终审强制拦截 + 管理员确认）**：点击「审核发布」时，后端先运行确定性护栏（别名归一化冲突、单字泛化词、手机号/邮箱/学号/条码与一次性个案措辞检测），再调用独立 AI 审核器复核隐私信息、个案残留、业务范畴重复与适用条件严谨度；任一不通过则强制驳回发布并给出具体修改建议。
   - **热重载生效**：通过双审后原子写入 `1. <标题>.md` 并即刻热刷新运行时 `SupportResponder` 的 `PresetCatalog`，无需重启服务。

## 2. Schema 升级（`SCHEMA_VERSION = 5`）

- `dashboard_users(user_id, username, display_name, password_hash, role, permissions_json, is_active, created_at_utc, updated_at_utc, last_login_utc)`
- `dashboard_sessions(token_hash, username, created_at_epoch, expires_at_epoch, last_seen_epoch)`
- `preset_drafts(draft_id, source_case_id, source_event_seq, source_question, staff_reply, mode, target_preset_id, preset_id, title, aliases_json, applicability, body, ai_generation_note, ai_review_status, ai_review_reasons_json, ai_review_suggestion, status, created_by, reviewed_by, created_at_utc, updated_at_utc, published_at_utc)`
- 兼容迁移补充列：`staff_cases.closed_by`、`staff_case_messages.operator_username`、`outbound_replies.operator_username`。
