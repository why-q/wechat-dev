# Step 3：Tier 1 别名/决策模型路由、Tier 2 `0.` 语料检索引用与澄清/Fallback 闭环

## 目标

接通 [图书馆智能客服设计](DESIGN.md) 自动问答决策链（紧接 [Step 2](STEP2-FILTER-AND-MERGE-PIPELINE.md) 的前置过滤、即时抢占/合批与未结待办判定）：

1. **`1.` 固定答案 YAML 规范与整句精确匹配（Tier 1a）**：[presets.py](../support_backend/presets.py) 读取 `1.*.md`（当前共 `3` 份：`图书续借`、`图书预约`、`开馆时间`）的 `id`、`title`、`aliases`、`applicability` frontmatter；使用统一归一化（`NFKC`、首尾去空白、连续空白压成单空格、循环剥离句末标点与符号、`.casefold()` 大小写折叠）做整句精确匹配，启动时校验别名无跨文档冲突，命中即直接返回正文。
2. **`/v1/systemone` 决策模型路由（Tier 1b）**：[decision_client.py](../support_backend/decision_client.py) 对接 `decision-model-preview`（站点地址自动规范拼接 `/v1/systemone`，按线上实测耗时 `1407~1975 ms` 校准为默认 `3.0s` 超时），单次多题 `Choice` 同时判定 `route`（`public_qa` / `clarify` / `staff_action` / `unknown`）与 `preset_id`。仅 `public_qa` 且 `preset_id != "none"` 且置信度 `>= SUPPORT_PRESET_MIN_CONFIDENCE`（默认 `0.65`）时直返 `1.` 正文；`clarify` 触发单次澄清；`staff_action` 先建待办再发兜底模板；`unknown` 直接回复兜底模板且**不建**馆员待办；`public_qa` 无适用预设、置信度不足、超时、报错或未配置时降级进 Tier 2。
3. **仅 `0.` 公开资料检索与核对引用（Tier 2）**：[knowledge.py](../support_backend/knowledge.py) 严格过滤 `corpus/` 下 `0.` 前缀文档，提取并剥离 frontmatter 中的 `source` URL 后，先用 `MarkdownNodeParser` 拆小节并将当前标题追加到 `header_path` 末尾，再用 `SentenceSplitter(paragraph_separator="\n\n", chunk_size=512, chunk_overlap=80)` 细分长小节。检索默认 `top_k=4`、`min_score=0.2`、超时 `25.0s`，剔除模型生成链接，并校验 `source_index` 严格指向本次检索节点。
4. **长度分流、单次澄清上限与兜底待办**：[responder.py](../support_backend/responder.py) 协同 [schema.py](../support_backend/schema.py) / [db.py](../support_backend/db.py)（`SCHEMA_VERSION=3` 新增 `clarification_states` 与 `triage_runs`）实现：
   - 短答直接返回，长答（`> SUPPORT_LONG_ANSWER_CHARS`，默认 `220`）先给简短结论再附 `具体请参考 {source_url} 下的「{section_title}」节。`；
   - 每个问题最多澄清 `1` 次（状态有效期 `1800s`，由 `Tier2KnowledgeResponder.generate_clarify_question` 生成追问，失败回退默认澄清句）；再次触发澄清直接走兜底模板并建 `staff_todo`；
   - `staff_action`、检索无据/异常或澄清耗尽时，**先落库 `staff_cases(case_kind='staff_todo')` 再尝试发信**，避免发送失败或额度受限丢失待办。

---

## 新增模块与配置

| 模块 / 接口 | 说明 |
|---|---|
| `support_backend/presets.py` | 解析 `1.*.md` frontmatter（含无 `PyYAML` 时的安全回退）、`normalize_alias_text`、别名碰撞检测。 |
| `support_backend/decision_client.py` | `/v1/systemone` HTTP JSON 客户端，兼容 `choice`/`noul`/`score`、默认 `3.0s` 超时控制及 URL 自动规范化。 |
| `support_backend/knowledge.py` & `__main__.py` | `0.` 语料建库与运行时检索/澄清 CLI（`python -m support_backend inspect-chunks \| build-index`）、NLTK 单硬链属主隔离缓存（兼容 systemd `DynamicUser=yes`）。 |
| `support_backend/responder.py` | Tier 1a -> Tier 1b -> Tier 2 -> 澄清/兜底编排，与 `commit_batch_generation` 提交屏障解耦：提交成功后才消费澄清状态、建待办并发信。 |
| `support_backend/schema.py` & `shared.py` | SQLite DDL（`SCHEMA_VERSION=3`）与跨模块复用的脱敏、环境变量和 UTF-8 截断工具。 |
| `GET /relay/support/triage-runs` | 查阅结构化路由与引用审计记录（支持 `limit=1..200`，返回 `{count, runs}`，需 Bearer 鉴权）。 |
| `GET /relay/support/presets` | 查看已加载的 `1.` 固定答案与别名清单（返回 `{count, presets}`，需 Bearer 鉴权）。 |

---

## 环境变量一览（统一收敛于 `/etc/wechat-relay.env`）

- `SUPPORT_DECISION_BASE_URL` / `SUPPORT_DECISION_API_KEY` / `SUPPORT_DECISION_MODEL`（默认 `decision-model-preview`）/ `SUPPORT_DECISION_TIMEOUT_SECONDS`（默认 `3.0`）
- `SUPPORT_CORPUS_DIR`（代码默认 `rag-poc/corpus`，systemd 指定 `/srv/wechat-relay/rag-poc/corpus`）、`SUPPORT_INDEX_DIR`（代码默认 `rag-poc/index_v2`，systemd 指定 `/srv/wechat-relay/rag-poc/index_v2`）
- `SUPPORT_PRESET_MIN_CONFIDENCE`（默认 `0.65`）、`SUPPORT_LONG_ANSWER_CHARS`（默认 `220`）
- `SUPPORT_AUTO_REPLY_ENABLED`（代码默认 `false`，线上环境开启 `true`）、`SUPPORT_AUTO_REPLY_ALLOWLIST`（逗号分隔 OpenID 列表）与 `SUPPORT_GRAY_ENROLL_KEYWORD`（私信自助加入白名单口令，未设置或置空时默认关闭，不硬编码口令）；两者任一非空即启用白名单门控，放行静态列表及 SQLite `auto_reply_allowlist` 动态入组读者，其余读者记为 `status='shadow'`。
- `SUPPORT_TIER2_ENABLED`（默认 `true`）、`SUPPORT_TIER2_TOP_K`（默认 `4`）、`SUPPORT_TIER2_MIN_SCORE`（默认 `0.2`）
- `RAG_NLTK_DATA_DIR`：systemd 下设为 `/var/lib/private/wechat-relay/nltk_data`，确保 `nltk.pathsec` 在 `DynamicUser=yes` 下满足单硬链与属主一致。

---

## 语料现状与待办（衔接第 4 步）

- `0.` 语料当前共 `5` 份文档、`67` 个块（已在真机测试中验证 `exact_alias`、`decision_preset`、`tier2_clarify -> tier2_short`、`feedback`、`emergency`、`staff_case_appended` 与白名单实发）。
- 现有 `0.` 语料缺少空间/静音舱/座位预约说明，相关提问会按设计触发 `tier2_fallback` 并生成 `staff_todo`；待第 4 步前补充对应 `0.` 公开资料、清理测试待办并重跑 `build-index`。

---

## 重建 `0.` 语料索引命令

```bash
rag-poc/.venv/bin/python -m support_backend build-index \
  --corpus rag-poc/corpus \
  --index-dir rag-poc/index_v2 \
  --env-file rag-poc/.env
```
