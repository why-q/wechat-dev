# Step 2：自有服务器 Filter、SQLite 持久化与消息调度管道

## 目标

在 [Step 1](STEP1-CLOUD-FORWARD-PIPELINE.md)「微信云托管 `/wechat` -> 自建服务器 `/relay/wechat` + 直连 `custom/send`」基础上，落地 [图书馆智能客服设计](DESIGN.md) 前半段（收到消息 -> 前置过滤 -> 即时抢占或防抖合批 -> 是否同一未结馆员待办）：

1. **前置过滤（`filters.py`）**：识别并直接回复 `emergency` 紧急联系方式、`feedback` 非紧急建议确认；跳过 `unsupported`（含菜单 `event:VIEW`、媒体、纯 URL 文本）与 `invalid`（空白、纯标点/表情、孤立寒暄/测试词）；其余有效 `question` 进入问答调度。
2. **双模式问答调度（`pipeline.py`）**：按 `(account_id, reader_id)` 维度与 `(account_id, MsgId)` 幂等去重。默认开启**即时抢占模式**（`SUPPORT_MERGE_ENABLED=false`），首条消息立即触发应答，提交前同读者连发消息则追加并抢占重跑；保留「`3` 秒静默 / `10` 秒上限」防抖合并模式（`SUPPORT_MERGE_ENABLED=true`）作可选开关。
3. **持久化、重启恢复与未结待办续接（`schema.py` / `db.py`）**：使用受限权限 SQLite WAL（`0600`）持久化入站事件、调度批次、反馈、待办、澄清与发信审计；重启时自动续跑未闭合 `processing`/`queued` 批次并冲刷或续调防抖 `open` 批次；就绪后检查是否存在同一未结 `staff_todo`，决定追加待办还是进入 [Step 3](STEP3-TIER1-AND-TIER2-RAG.md) 自动分诊。

---

## SQLite 表结构（`support_backend/schema.py` & `support_backend/db.py`）

数据库路径由 `SUPPORT_DB_PATH` 指定（未设置时默认落在 `RELAY_LOG_PATH` 同目录的 `library-support.db`；systemd 环境为 `/var/lib/private/wechat-relay/library-support.db`，目录 `0700`、文件 `0600`，当前 `SCHEMA_VERSION=4`）：

| 表名 | 核心字段 | 职责 |
|---|---|---|
| `inbound_events` | `seq`, `dedup_key UNIQUE`, `received_at_epoch`, `account_id`, `reader_id`, `msg_type`, `event`, `msg_id`, `content`, `filter_category`, `filter_reason`, `batch_id`, `staff_case_id`, `feedback_id`, `expires_at_epoch` | 全量入站审计、`msg:{account_id}:{msg_id}` 幂等去重、7 天过期时间基准（对齐 [数据集方案](RAG-DATASET-TEST-PLAN.md)）。 |
| `merge_batches` | `batch_id`, `account_id`, `reader_id`, `status (open\|processing\|queued\|flushed)`, `first_received_at_epoch`, `last_received_at_epoch`, `quiet_deadline_epoch`, `max_deadline_epoch`, `flush_at_epoch`, `message_count`, `merged_text`, `flushed_at_epoch`, `route_stage`, `staff_case_id`, `generation`, `preempt_count`, `preempt_locked` | 通过 `idx_merge_active_reader` 与 `idx_merge_queued_reader` 部分唯一索引保证单读者至多一个未锁定活动批与一个排队批。 |
| `batch_messages` | `id`, `batch_id`, `event_seq UNIQUE`, `received_at_epoch`, `text_content` | 记录构成批次的去重文本，按 `(received_at_epoch, event_seq)` 顺序拼接。 |
| `feedback_records` | `feedback_id`, `account_id`, `reader_id`, `event_seq`, `content`, `status`, `staff_note` | 存储非紧急建议反馈供看板展示（不阻塞后续正常提问）。 |
| `staff_cases` | `case_id`, `account_id`, `reader_id`, `source_batch_id`, `source_event_seq`, `case_kind (staff_todo\|emergency)`, `status (open\|closed)`, `summary`, `latest_message`, `closed_at_utc`, `resolution_note` | 馆员待办与紧急事件记录，`open` 的 `staff_todo` 参与未结案件续接判定。 |
| `staff_case_messages` | `item_id`, `case_id`, `batch_id`, `event_seq`, `sender_role`, `content` | 记录追加到未结待办上的读者补充消息及处理时间线。 |
| `outbound_replies` | `reply_id`, `account_id`, `reader_id`, `trigger_kind`, `trigger_ref_id`, `content`, `delivery_mode`, `status`, `errcode`, `errmsg`, `duration_ms` | 审计每次真实发送（`sent` / `failed`）或影子记录（`shadow`）。 |
| `auto_reply_allowlist` | `account_id`, `reader_id`, `source`, `created_at_utc`, `updated_at_utc` | 持久化通过环境变量配置的灰度口令自助加入自动回复白名单的读者。 |
| `clarification_states` / `triage_runs` | `clarify_id`, `status`, `expires_at_epoch` / `run_id`, `stage_path`, `decision_*` | Step 3 单次澄清状态机与完整分诊审计（详见 [Step 3 文档](STEP3-TIER1-AND-TIER2-RAG.md)）。 |

---

## Filter 分类与未结待办判定（`support_backend/filters.py`）

前置分类按以下优先级判定：

1. **`unsupported`**：`msg_type != "text"`（如微信菜单 `event:VIEW`、`subscribe`、图片/语音/链接等），或文本仅由一个或多个 URL 构成。
2. **`invalid`**：空白文本、纯标点/装饰符、纯微信表情方括号/斜杠代码（如 `[微笑]`），或孤立寒暄/测试词（如 `你好`、`在吗`、`hi`、`test`、`1`）。包含实质提问的句子（如 `你好，请问几点关门`）仍归为 `question`。
3. **`gray_enroll`**：仅当配置了非空 `SUPPORT_GRAY_ENROLL_KEYWORD` 时启用（未设置时默认关闭，代码中不硬编码口令）。整句匹配该口令（忽略首尾标点/引号/空格及微信表情）后原子写入 `auto_reply_allowlist` 并立即回执确认，不进入问答合批、不追加未结待办且不消耗澄清额度。
4. **`emergency`**：物理被困、人身安全、突发医疗、火警水患漏电或现场治安危机，同时排除 `储物柜`、`账号`、`借阅卡` 等非物理锁定误报。命中后立即回复与 `0. 联系我们.md` 对齐的预设紧急联系方式并创建 `staff_cases(case_kind='emergency')`。
5. **`feedback`**：非紧急读者建议或表扬（如 `建议二楼增加插座`、`表扬工作人员`），且未包含系统报错、现场投诉或荐购词（`推荐购买新书` / `荐购` 归为 `question`）。命中后立即写入 `feedback_records` 并回复收件确认。
6. **`question`**：其余所有有效文本（含短补充片段），进入调度管道。

**未结待办续接（`default_same_case_matcher`）**：批次就绪后，若同读者名下存在 `open` 状态 `staff_todo`，当新文本命中追问标记词（如 `还是不行`、`补充一下`、`学号`）、与原待办双字词重合度 `>= 2`，或处于 `30` 分钟（`1800s`）活跃窗口内的短补充（`<= 40` 字）且不匹配明确新话题词时，直接追加到该待办并停止自动回复，交由馆员处理。

---

## 调度双模式与发信灰度（`support_backend/pipeline.py`）

通过环境变量 `SUPPORT_MERGE_ENABLED` 切换普通提问的调度策略：

1. **`SUPPORT_MERGE_ENABLED=false`（即时启动 + 抢占重跑，当前默认）**：
   - 依据线上真实留言样本（绝大多数读者仅发单条提问，连续发言者均不超过 `3` 条），关闭强制 `3` 秒等待以降低常规首答延迟。
   - 首条 `question` 到达即建 `status='processing'`、`generation=1` 批次并立刻启动异步分诊。
   - 若提交前同读者又发来新 `question` 且未超 `SUPPORT_IMMEDIATE_MAX_PREEMPTS`（默认 `2`，即单轮最多合入 `3` 条）与 `SUPPORT_MAX_WAIT_SECONDS`（默认 `10.0s`），则将新文本并入当前批次、`generation += 1`、`preempt_count += 1`，取消旧任务并启动新一代重跑。
   - **提交屏障**：仅当 `commit_batch_generation` 原子核对 `status='processing' AND generation=expected` 通过后才闭合批次并落库/发信，过期代次静默丢弃；达上限后置 `preempt_locked=1` 锁定当前批次完成答复，后续消息自动写入 `queued` 批次排队接力。
2. **`SUPPORT_MERGE_ENABLED=true`（防抖合并模式，可选回退）**：
   - 首条 `question` 创建 `open` 批次（`flush_at = min(now + 3s, first + 10s)`）；截止前收到新文本重置 `3` 秒静默计时但不延长 `10` 秒上限，由单周期 `asyncio` 调度器统一到期冲刷。
- **出站发信与灰度**：代码中 `SUPPORT_AUTO_REPLY_ENABLED` 默认 `false`（写 `status='shadow'`）。当前生产环境 `/etc/wechat-relay.env` 已启用 `SUPPORT_AUTO_REPLY_ENABLED=true` 并通过 `SUPPORT_AUTO_REPLY_ALLOWLIST` 限定仅白名单（含静态白名单与通过 `SUPPORT_GRAY_ENROLL_KEYWORD` 动态加入的 `auto_reply_allowlist` 记录）真实调用 `custom/send`，其余读者全流程记录 `shadow`。

---

## 可观察与联调接口（均需 `RELAY_AUTH_TOKEN` Bearer 鉴权）

- `POST /relay/wechat`：校验 `wx_headers`、同步落盘 JSONL 与 SQLite，并异步调度 Filter/问答回复（返回 `{ok, accepted, duplicate, seq}`）。
- `GET /relay/support/overview`：返回分类计数、活跃批次与各表统计（脱敏，不含 OpenID 与原文）。
- `GET /relay/support/events`、`batches`、`feedbacks`、`cases`、`replies`、`allowlist`：按 `limit=1..200` 及可选 `category` / `status` 检索结构化状态（Step 3 另增 `triage-runs` 与 `presets`）。
- `POST /relay/support/cases`、`POST /relay/support/cases/{case_id}/close`、`POST /relay/support/allowlist/remove`：手工创建/结案馆员待办或移出动态灰度白名单。
- `POST /relay/support/flush-due`：手动冲刷到期防抖批次。
