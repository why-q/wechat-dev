# 图书馆 AI 客服：项目背景与现状

## 1. 工作背景与业务目标

本人在汕头大学图书馆负责数字化建设相关工作，具体职责目前就是负责 AI 应用方面的开发。

本项目起因是图书馆读者投诉较多，据观察大部分与未充分了解图书馆规章有关（暂无投诉分类和数量统计可用于量化），希望先由 AI 解答有公开依据的通用问题（实现中），其余无法回答的问题（或无信心回答的问题）再转接人工（转接部分尚未实现）。

| 渠道 | 已知情况 | 本次范围 |
|---|---|---|
| 客服投诉 | 存在独立渠道，系统及接入方式尚未说明 | 业务目标之一，尚未开展技术核实 |
| 校长邮箱投诉 | 图书馆无访问权限 | 暂不作为本次重点 |
| 公众号投诉或咨询 | 读者在公众号聊天框发私信，工作人员通过微信公众平台自带后台处理 | 当前通过云托管消息推送接入 |

注意：公众号入口暂时不含文章底部评论，不使用第三方客服。

## 2. 数据范围

答案以图书馆公开文档为依据，包括图书馆主页和公众号文章等。早期 RAG 基线使用 4 份资料，本次客服设计依据更新后的 11 份 corpus 文档。服务范围限通用问题和公开自助指引，个人业务情况由馆员或读者本人核实。

仍需区分两类诉求：

- 咨询规定：公开文档可能足以回答。
- 投诉处理、对规定有异议、需要工作人员采取行动：即使文档有规定，也不能据此判定投诉已解决，需要转人工。

公众号收发消息仍涉及 OpenID 和消息正文，后续需决定日志、保存期限和对外模型调用的数据边界，初期实验阶段暂不考虑。

## 3. 开发环境

在当前服务器进行开发并使用 GitHub 进行版本管理，使用微信云托管将最新代码版本从 GitHub 同步到微信托管服务器上进行服务更新。

## 4. 测试环境

除项目自带测试之外，使用正式微信服务号人工进行真实测试（链路已经过测试，无问题）。

## 5. 历史记录

2026-11-25: 试图使用微信的 `getMsgList` 接口来获取用户的询问，无果。因为这个接口只会提取到已经被回复的消息。如果没有回复用户（通过后台或者其他任何方式），则消息不会被捕获。

初步测试方案：公开、现行的图书馆资料作为检索语料。先手工标注问题，冻结离线评估集，再通过微信云托管 `/wechat` 回调前向采集待审核问题。读者原文在受限环境短期保存，获准的脱敏改写经人工标注后进入评估集。建议使用 Baserow 审核，或先用本地表格起步，评估使用带版本的导出文件。[RAG 数据集与测试方案](docs/RAG-DATASET-TEST-PLAN.md)

客服设计提案：先按读者诉求选择已审核标准答案、一次澄清、人工入口或紧急指引。RAG 用新版语料进行离线对照，达到质量与覆盖门槛后按主题试点。通过运营看板跟踪答复、人工处理与知识缺口，馆员审核后将失败样本和修订内容纳入回归测试。提案含样本分析和首批待审答案。 [图书馆智能客服设计](docs/LIBRARY-SUPPORT-DESIGN.md)

### 2026-11-26 · 客服流程讨论

- `Feedback` 指读者主动提出的非紧急建议，直接展示在 Dashboard。`Emergency` 现阶段向读者提供联系方式，值班通知渠道待补充。
- 自动问答无法给出可靠答复时使用 `Fallback` 模板。读者的预约一直报错、系统无法解答时，先回复 `Fallback` 模板，再生成馆员待办。
- 引用地址取 `0.xxx` 文档的 frontmatter `source`。《规章制度》的地址由项目负责人更新。建索引时先提取 URL、剥离 YAML，再切分正文。每个检索块保存 `source` 和包含当前小节标题的完整 `header_path`。长答复先给简短结论，再附「具体请参考 [LINK] 下的 xxx 节。」链接和小节来自实际支撑结论的资料。
- Tier 1 对合并后的普通问题做归一化与整句别名匹配，未命中时由 Jev 判断诉求，并根据 `1.` 固定答案的正文和适用条件判断能否覆盖完整问题。命中适用答案时直返正文。缺少适用答案的公开咨询和通用排障进入 Tier 2，向量索引仅收录文件名以 `0.` 开头的原始资料，引用来自支撑结论的小节。
- `Build Database` 有两条路径。一条补充评测问题集，另一条收集工作人员反馈。
- 微信云托管接收公众号消息并转发到自有服务器。后端业务处理、Dashboard 和 Database 均在自有服务器运行。
- 馆员在微信公众号后台回复读者。自有服务器的 Dashboard 展示待办和处理结果，并支持记录结果。Dashboard 直接回复和自有服务器回发微信链路待确认。
- 同一未结事项的后续消息追加到已有待办，由接手的馆员通过微信公众号后台回复并在 Dashboard 结案。公众号后台人工回复独立于服务号客服 API 发送额度。若后续 Dashboard 通过客服 API 发信，其消息与自动回复一起计入额度。

### 2026-11-26 · 云托管转发测试管道

- 第一步实现云托管到自有服务器的转发测试管道：`app.py` 新增 `WECHAT_FORWARD_URL`、`WECHAT_FORWARD_TOKEN`、`WECHAT_FORWARD_TIMEOUT_SECONDS`（默认 3.0s，上限 4.5s）和 `WECHAT_FORWARD_FAIL_OPEN`（默认 true），在 `CheckContainerPath` 之外的所有 `/wechat` 回调中同步转发（先于 `WECHAT_AUTO_REPLY_ENABLED` 提前返回，避免试测阶段依赖 RAG/LLM 凭据或触发错误自动回复）。
- 自有服务器侧新增独立接收端 `relay_receiver.py`，强制要求 `RELAY_AUTH_TOKEN` Bearer 鉴权，提供 `/healthz`、`/relay/wechat` 与 `/relay/events`，并将收到的消息及 `x-wx-*` 头以 `0600` 权限持久化到 JSONL（默认 `data/relay-events.jsonl`，已加入 `.gitignore`）。具体操作见 [云托管转发测试管道](docs/CLOUD-FORWARD-TEST-PIPELINE.md)。
- 首次实测确认云托管公网转发成功：事件与文本消息均以 JSON 送达，携带 `x-wx-appid`、`x-wx-env`、`x-wx-openid`、`x-wx-region`、`x-wx-service`、`x-wx-source`（实测值为 `other`），且 `event` 类型无 `MsgId`。
- 下一步在 `app.py` 增加可选云头校验（`WECHAT_CLOUD_EXPECTED_SOURCE` / `ENV` / `APPID`）与 `errmsg` 安全日志，在 `relay_receiver.py` 新增直连 `custom/send` 测试接口 `/relay/send-test` 及受控云调用回发测试钩子，并配套 `wechat-relay.service` 与 Nginx HTTPS（`wechat-dev.llink.cyou`）配置以完成出站手机送达验收。
- **2026-11-26 双向闭环实测通过**：已确认「手机私信 -> 微信云托管 -> HTTPS 自有服务器（`wechat-relay.service`）-> 自有服务器直连 `custom/send` -> 手机真实收到回复」全链路跑通；后续消息处理与回发可全部驻留自有服务器。同时观测到公众号日常会产生大量 `msg_type="event"`（如自定义菜单 `Event="VIEW"`，无 `Content` 和 `MsgId`），需在下一步的 `Filter` 中按 [图书馆智能客服设计](docs/LIBRARY-SUPPORT-DESIGN.md) 过滤。

### 2026-11-26 · 第二步：Filter、SQLite 与消息合并管道

- **自有服务器核心存储与分流落地**：新增 `support_backend/`（`db.py`、`filters.py`、`pipeline.py`），在自有服务器使用 Python 标准库 SQLite WAL（`0600` 权限，systemd 默认路径 `/var/lib/private/wechat-relay/library-support.db`）持久化入站事件、`merge_batches`、`batch_messages`、`feedback_records`、`staff_cases` 与 `outbound_replies`。
- **Filter 与护栏**：覆盖 `unsupported`（含菜单 `event:VIEW`、媒体及纯 URL 文本）、`invalid`（空白、纯标点/表情及孤立寒暄）、`emergency`（物理被困/安防医疗关键词并排除 `储物柜/账号被锁` 误报，立刻回报 [联系我们](rag-poc/corpus/0.%20联系我们.md) 热线）、`feedback`（非紧急建议记入看板并回执，不拦截 `推荐购买新书` 荐购咨询或现场投诉），其余有效文本进入 `question` 合并窗口。
- **3s 静默 / 10s 上限合并与重启恢复**：按 `(account_id, reader_id)` 建唯一 `open` 批次并基于服务端接收时间计时；按 `(account_id, MsgId)` 去重且重复投递不重置计时；服务重启自动立即冲刷已逾期批次并按剩余秒数续调未到期批次；冲刷后检查 `Same Unresolved Staff Case?` 决定追加已有 `staff_todo` 待办或进入后续分诊。
- **安全影子默认模式**：`wechat-relay.service` 默认配置 `SUPPORT_AUTO_REPLY_ENABLED=false`（将回复写入 `outbound_replies(status='shadow')`，暂不主动对外发送），并已重启实测捕获线上真实 `event:VIEW` 推送且保持 `49` 项单测全通过；详细设计与新增 `/relay/support/*` 接口见 [Step 2 文档](docs/STEP2-FILTER-AND-MERGE-PIPELINE.md)。
- **2026-11-26 · 实测消息分布与立即抢占模式切换为默认**：项目负责人指出在现有 80 余名读者留言样本中仅 2 名发出多条连续消息（且均不超过 3 条）；强制对全部读者等待 3 秒会在多数单条提问上引入无谓延迟。据此已将系统与 `wechat-relay.service` 默认值切换为 `SUPPORT_MERGE_ENABLED=false`（立即启动 + 未提交前新消息追加并取消重跑），`SUPPORT_MERGE_ENABLED=true` 保留作可选 3s/10s 合并回退；配合 `SUPPORT_IMMEDIATE_MAX_PREEMPTS=2`（对应最多 3 条连续输入，超限锁批并入 `queued` 接力）与 `generation` 提交屏障防止双发与过期写入，并兼容迁移线上既有 SQLite 数据库（`SCHEMA_VERSION=2`）。
- **2026-11-26 · Tier 1 决策模型选型（`decision-model-preview`）**：确认国内/可用中转已提供兼容 `/v1/systemone` 协议的 `decision-model-preview` 模型，原生支持 `choice`、`noul`、`score` 多题单次调用并返回各选项概率与置信度（仅按输入 token 计费；`base_url` 填站点根地址不带 `/v1`）。第 3 步 Tier 1 路由将直接对接 `POST {base_url}/v1/systemone` 一次同时判定下一步分流与 `1.` 固定答案匹配，保留 `2s` 超时降级到 Tier 2 `0.` 语料 RAG。

### 2026-11-26 · 第三步：Tier 1 别名/决策路由、Tier 2 仅 `0.` 检索与澄清/Fallback 闭环

- **Tier 1a 固定答案与归一化**：为 [1. 图书续借.md](rag-poc/corpus/1.%20图书续借.md)、[1. 图书预约.md](rag-poc/corpus/1.%20图书预约.md)、[1. 开馆时间.md](rag-poc/corpus/1.%20开馆时间.md) 补充 `id`、`title`、`aliases`、`applicability` frontmatter；[presets.py](support_backend/presets.py) 实现对称 `NFKC` + 空白折叠 + 去句末标点装饰符匹配与别名冲突校验（`1.` 正文严格保留作固定直返，不进入 Tier 2 向量库）。
- **Tier 1b `/v1/systemone` 路由**：[decision_client.py](support_backend/decision_client.py) 对接 `decision-model-preview`（站点 URL 自动规范拼接 `/v1/systemone`、2s 超时），单次返回 `route` 与 `preset_id`（含 `confidence` 阈值门控）；超时、报错或未配置时按设计直接降级进 Tier 2。
- **Tier 2 仅 `0.` 资料分块与核对引用**：[knowledge.py](support_backend/knowledge.py) 仅收录 `0.` 前缀公开文档，提取 `source` 后剥离 YAML，采用 `MarkdownNodeParser` 生成小节并将当前标题追加到 `header_path` 末尾，超长小节按 `SentenceSplitter(paragraph_separator="\n\n", chunk_size=512, chunk_overlap=80)` 细分；实际建成 `rag-poc/index_v2`（`5` 份 `0.` 文档、`66` 个块，含完整 `source` 与 `header_path` 元数据，`.gitignore` 已忽略生成索引文件）。
- **长短分流、单次澄清上限与待办兜底**：[responder.py](support_backend/responder.py) 配合 `SCHEMA_VERSION=3`（新增 `clarification_states` 与 `triage_runs`）将逻辑严密拆分为「可抢占取消的 `triage`」和「提交屏障后的 `apply_committed_plan`」，长答按「简短结论 + `具体请参考 {URL} 下的「{section_title}」节。`」输出，每个问题最多反问 `1` 次，需要人工跟进时先写 `staff_todo` 再发信；systemd 服务已切至 `rag-poc/.venv`、设置 `HOME=/var/lib/private/wechat-relay` 与 `AF_UNIX` 并隔离 `DynamicUser` NLTK 缓存，线上重启实测 `tier2_ready=true`、`presets=3`。详见 [Step 3 文档](docs/STEP3-TIER1-AND-TIER2-RAG.md)。

## Reference

- [消息与事件推送介绍](https://developers.weixin.qq.com/doc/subscription/guide/dev/push/)
- [接收事件推送](https://developers.weixin.qq.com/doc/subscription/guide/product/message/Receiving_event_pushes.html)
- [获取聊天记录](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_getmsglist)
