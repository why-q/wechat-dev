# Step 2：自有服务器 Filter、SQLite 持久化与消息合并管道

## 目标

在 Step 1 已验证的「微信云托管 `/wechat` -> 自有服务器 HTTPS `/relay/wechat` + 直连 `custom/send`」基础上，落地 [图书馆智能客服设计](LIBRARY-SUPPORT-DESIGN.md) 前半段（Message -> Filter -> Merge Messages -> Same Unresolved Staff Case?）：

1. **前置过滤（Filter）**：快速识别并发放 `Emergency` 紧急联系方式、`Feedback` 非紧急建议记录与确认、跳过 `Unsupported Format`（含微信菜单 `event:VIEW`、多媒体、纯 URL 文本）与 `Invalid`（空白、纯标点/表情符号、孤立问候语或测试噪点），将有效 `Question` 送入合并队列。
2. **消息合并（Merge Messages）**：按 `(account_id, reader_id)` 维度，以自有服务器接收时间计时，实现「末条新普通文本后静默 3 秒」或「首条新普通文本后满 10 秒」（先到为准）的防抖合并；按 `MsgId` 去重，重复重试不重置计时。
3. **重启恢复与未结待办分流（SQLite + Same Unresolved Staff Case?）**：使用受限权限 SQLite WAL（`0600`）持久化入站消息、合并批次、反馈记录、馆员未结待办与出站发信审计；服务重启时自动续接未到期批次并立即冲刷已到期批次；批次冲刷后检查同一读者是否存在未结馆员待办并决定追加还是进入后续分诊。

---

## SQLite 表结构（`support_backend/db.py`）

数据库默认路径：环境变量 `SUPPORT_DB_PATH`（若未显式设置，则落在 `RELAY_LOG_PATH` 同目录的 `library-support.db`；systemd 环境为 `/var/lib/private/wechat-relay/library-support.db`，目录 `0700`、文件 `0600`）。

| 表名 | 核心字段 | 职责 |
|---|---|---|
| `inbound_events` | `seq`, `dedup_key UNIQUE`, `received_at_epoch`, `account_id`, `reader_id`, `msg_type`, `event`, `msg_id`, `content`, `filter_category`, `filter_reason`, `batch_id`, `staff_case_id`, `feedback_id`, `expires_at_epoch` | 全量入站审计、`msg:{account_id}:{msg_id}` 幂等去重、7 天过期时间基准（对齐 [数据集方案](RAG-DATASET-TEST-PLAN.md)）。 |
| `merge_batches` | `batch_id`, `account_id`, `reader_id`, `status (open\|flushed)`, `first_received_at_epoch`, `last_received_at_epoch`, `quiet_deadline_epoch`, `max_deadline_epoch`, `flush_at_epoch`, `message_count`, `merged_text`, `flushed_at_epoch`, `route_stage`, `staff_case_id` | `idx_merge_open_reader` 部分唯一索引保证同一 `(account_id, reader_id)` 同时至多一个 `open` 批次。 |
| `batch_messages` | `id`, `batch_id`, `event_seq UNIQUE`, `received_at_epoch`, `text_content` | 记录构成批次的有序去重文本，按 `(received_at_epoch, event_seq)` 合并。 |
| `feedback_records` | `feedback_id`, `account_id`, `reader_id`, `event_seq`, `content`, `status`, `staff_note` | 存储 `Feedback` 非紧急建议供 Dashboard 展示（不阻塞后续正常提问）。 |
| `staff_cases` | `case_id`, `account_id`, `reader_id`, `source_batch_id`, `source_event_seq`, `case_kind (staff_todo\|emergency)`, `status (open\|closed)`, `summary`, `latest_message`, `closed_at_utc`, `resolution_note` | 馆员待办与紧急事件记录，`open` 的 `staff_todo` 参与 `Same Unresolved Staff Case?` 判定。 |
| `staff_case_messages` | `item_id`, `case_id`, `batch_id`, `event_seq`, `sender_role`, `content` | 未结待办上的读者补充消息及系统/馆员处理时间线。 |
| `outbound_replies` | `reply_id`, `account_id`, `reader_id`, `trigger_kind`, `trigger_ref_id`, `content`, `delivery_mode`, `status`, `errcode`, `errmsg`, `duration_ms` | 审计每一次真实发送或影子（shadow）记录，保留失败原因。 |

---

## Filter 分类规则（`support_backend/filters.py`）

按如下优先级判定：

1. **`unsupported`**：
   - `msg_type != "text"`（含 `event` 如 `VIEW`、`subscribe`、`CLICK`，以及 `image`、`voice`、`video`、`shortvideo`、`location`、`link` 等）；
   - `msg_type == "text"` 但去除空白后仅由一个或多个 URL 构成（如纯 `https://...`）。
2. **`invalid`**：
   - 空白文本、纯标点符号/装饰符、纯微信表情方括号代码（如 `[微笑]`）；
   - 孤立测试词/寒暄（如 `你好`、`在吗`、`hi`、`hello`、`test`、`1`、`？`）。注意带实质内容的句子如 `你好，请问几点关门` 必须归为 `question`。
3. **`emergency`**：
   - 物理被困/人身安全/突发医疗/火警水患漏电/现场治安危机（如「我被锁在图书馆了」、停电/火灾/漏水/晕倒等）。
   - 显式排除借阅卡、账号密码、储物柜等业务词造成的误报（如 `怎么锁定储物柜`、`账号被锁了` 归为 `question`）。
   - 立即触发 `Return Contact`（引用 `rag-poc/corpus/0. 联系我们.md` 真实号码）并写入 `staff_cases(case_kind='emergency')`，不进入普通合并队列。
4. **`feedback`**：
   - 非紧急 Reader 建议、意见或表扬（如 `建议二楼增加插座`、`反馈一下空调太冷`、`表扬工作人员`），且未夹带明确排障/投诉现场处置/荐购问题；
   - 立即写入 `feedback_records` 并触发 `Feedback Received` 回执。
   - **例外**：`推荐购买新书` / `荐购` 归为 `question`（馆藏荐购属于公开业务咨询）；投诉处理或系统报错归为 `question`。
5. **`question`**：
   - 其余所有有效普通文本（含短追问片段如 `东海岸校区`、`还是不行`），进入 `Merge Messages`。

---

## Merge Messages 时序、双模式开关与安全运行

通过环境变量 `SUPPORT_MERGE_ENABLED` 切换两种普通问答调度策略：

1. **`SUPPORT_MERGE_ENABLED=true`（合并调度 / Debounce 模式）**：
   - 收到去重后的新 `question` 时，若已有 `open` 批次且 `now >= batch.flush_at_epoch`（已到截止但定时器尚未运行），先原子冲刷旧批次，再为新消息创建新批次；若 `now < batch.flush_at_epoch`，追加进该 `open` 批次并更新 `quiet_deadline = now + 3s`、`flush_at = min(quiet_deadline, max_deadline)`，`max_deadline = first + 10s` 保持不变。
   - `SupportPipeline` 在后台维护单周期 `asyncio` 调度任务；重启加载时立即冲刷 `flush_at <= now` 的遗留批次，并对未到期批次按剩余秒数重新调度。

2. **`SUPPORT_MERGE_ENABLED=false`（立即启动 + 抢占重跑模式 / Immediate 模式，当前默认）**：
   - 依据真实读者留言分布（约 80 余名读者中仅 2 名发出多条连续消息，且每名不超过 3 条），为避免 3 秒静默等待拖慢绝大多数单条提问用户，默认关闭延迟合并窗口。
   - 首条 `question` 到达即创建 `status='processing'`、`generation=1` 批次并立刻启动异步应答任务。
   - 若回答提交前同读者又发来新 `question` 且未超 `SUPPORT_IMMEDIATE_MAX_PREEMPTS`（默认 `2`，即单轮最多合并 3 条）和 `max_wait_seconds`，则把新文本并入该批次、`generation += 1`、`preempt_count += 1`，立即取消旧异步任务并启动新一代重跑。
   - **提交屏障防双发与防饿死**：唯有在 `commit_batch_generation` 原子核对 `status='processing' AND generation=expected` 通过后才闭合批次并发送/记待办；过期代次静默丢弃。达到重跑上限或超时上限时将当前批次锁定（`preempt_locked=1`）让其正常答复，后续新消息自动写入 `queued` 批次排队接力。
   - 重启恢复时自动续跑所有未闭合的 `processing` 及孤立 `queued` 批次。

- **出站发信开关（默认安全影子模式）**：`SUPPORT_AUTO_REPLY_ENABLED` 默认 `false`（仅写入 `outbound_replies(status='shadow')`，不向读者发真实客服消息），待 Step 3 接通后再开启 `true` 通过自有服务器直连 `custom/send` 发信。

---

## 可观察与联调接口（均需 `RELAY_AUTH_TOKEN` Bearer 鉴权）

- `POST /relay/wechat`：保持 Step 1 响应契约不变（`{ok, accepted, duplicate, seq}`），内部同步落盘 JSONL + SQLite，并触发 Filter 与异步合并调度。
- `GET /relay/support/overview`：各分类计数、活跃 `open` 批次数、最近待办/反馈/发信统计（不返回 OpenID 与原始文本）。
- `GET /relay/support/events`、`GET /relay/support/batches`、`GET /relay/support/feedbacks`、`GET /relay/support/cases`、`GET /relay/support/replies`：按 `limit` 查看结构化处理状态。
- `POST /relay/support/flush-due`：手动触发到期批次冲刷（便于离线排障与验收）。
- `POST /relay/support/cases/{case_id}/close`：手工结案待办并记录 `resolution_note`，用于验证后续消息不再追加到已结案件。
