# Step 3：Tier 1 别名/决策模型路由、Tier 2 `0.` 语料检索引用与澄清/Fallback 闭环

## 目标

接通 [图书馆智能客服设计](LIBRARY-SUPPORT-DESIGN.md) 完整自动问答决策链（紧接 Step 2 的 `Filter` 与 `Merge / Immediate` 调度）：

1. **`1.` 固定答案 YAML 规范与整句精确匹配（Tier 1a）**：[presets.py](../support_backend/presets.py) 为 `1.*.md` 补充 `id`、`title`、`aliases`、`applicability` frontmatter；使用统一归一化（`NFKC`、首尾去空白、连续空白压成单空格、剥离句末标点与装饰符）整句精确匹配，命中且无别名冲突时直接返回正文（0 延迟、0 API 费用）。
2. **`/v1/systemone` 决策模型路由（Tier 1b）**：[decision_client.py](../support_backend/decision_client.py) 对接 `decision-model-preview`（站点根地址规范化拼装 `/v1/systemone`，按线上实测 `1407–1975 ms` 校准为默认 `3.0s` 超时），单次多题 `Choice` 同时判定 `route`（`public_qa` / `clarify` / `staff_action` / `unknown`）与 `preset_id`（对应 `1.` ID 或 `none`）。超时、报错或未配置时按设计自动降级进 Tier 2。
3. **仅 `0.` 公开资料检索与核对引用（Tier 2）**：[knowledge.py](../support_backend/knowledge.py) 严格过滤 `corpus/` 下以 `0.` 开头的文档，提取并剥离 YAML frontmatter 中的 `source` URL，先按 `MarkdownNodeParser` 拆小节并将当前标题追加到祖先 `header_path` 末尾，再对超长小节用 `SentenceSplitter(paragraph_separator="\n\n", chunk_size=512, chunk_overlap=80)` 沿段落边界细分。
4. **长度分流、单次澄清上限与兜底待办**：[responder.py](../support_backend/responder.py) 协同 [db.py](../support_backend/db.py)（`SCHEMA_VERSION=3` 新增 `clarification_states` 与 `triage_runs`）实现：
   - 短答直接返回，长答（`> SUPPORT_LONG_ANSWER_CHARS`，默认 `220`）先给简短结论再附 `具体请参考 {source_url} 下的「{section_title}」节。`（URL 与小节标题均取自可核对元数据，模型生成的链接一律剔除）；
   - 每个问题最多澄清 `1` 次；再次触发澄清直接走 `Fallback` 模板并建 `staff_todo`；
   - `staff_action`、缺证据或澄清耗尽时**先落库 `staff_cases(case_kind='staff_todo')` 再尝试发信**，即使发送失败也不丢失馆员待办。

---

## 新增模块与配置

| 模块 / 配置 | 说明 |
|---|---|
| `support_backend/presets.py` | 解析 `1.*.md` frontmatter（含无 `PyYAML` 时的安全回退）、`normalize_alias_text`、别名碰撞检测。 |
| `support_backend/decision_client.py` | `/v1/systemone` HTTP JSON 客户端，兼容 `choice`/`noul`/`score`、默认 `3.0s` 超时控制及 URL 自动规范化。 |
| `support_backend/knowledge.py` | `0.` 语料建库与运行时检索回答 CLI（`python -m support_backend inspect-chunks \| build-index`）、NLTK 单链属主隔离缓存（兼容 systemd `DynamicUser=yes`）。 |
| `support_backend/responder.py` | Tier 1a -> Tier 1b -> Tier 2 -> 澄清/Fallback 编排，与 Step 2 的 `commit_batch_generation` 提交屏障严格解耦：仅在提交成功后消费澄清状态、建待办并发信。 |
| `support_backend/schema.py` & `shared.py` | SQLite DDL（`SCHEMA_VERSION=3`）与跨模块复用的脱敏、环境变量和 UTF-8 边界工具。 |
| `GET /relay/support/triage-runs` | 查阅结构化路由与引用审计记录（支持 `limit=1..200`，返回 `{count, runs}`，需 Bearer 鉴权）。 |
| `GET /relay/support/presets` | 查看已加载的 `1.` 固定答案与别名清单（返回 `{count, presets}`，含 `preset_id`/`title`/`aliases`/`applicability`，需 Bearer 鉴权）。 |

---

## 环境变量一览（统一收敛于 `/etc/wechat-relay.env`）

- `SUPPORT_DECISION_BASE_URL` / `SUPPORT_DECISION_API_KEY` / `SUPPORT_DECISION_MODEL`（默认 `decision-model-preview`）/ `SUPPORT_DECISION_TIMEOUT_SECONDS`（按线上实测耗时校准为默认 `3.0`）
- `SUPPORT_CORPUS_DIR`（由 systemd 指定 `/srv/wechat-relay/rag-poc/corpus`）、`SUPPORT_INDEX_DIR`（由 systemd 指定 `/srv/wechat-relay/rag-poc/index_v2`，仅 `0.` 索引）
- `SUPPORT_PRESET_MIN_CONFIDENCE`（默认 `0.65`）、`SUPPORT_LONG_ANSWER_CHARS`（默认 `220`）
- `SUPPORT_AUTO_REPLY_ENABLED`（代码默认 `false`，线上环境开启 `true`）与 `SUPPORT_AUTO_REPLY_ALLOWLIST`（可选逗号分隔 OpenID 列表；仅白名单用户触发真实 `custom/send`，其余读者保留 `status='shadow'` 记录）
- `SUPPORT_TIER2_ENABLED`（默认 `true`）、`SUPPORT_TIER2_TOP_K`（默认 `4`）、`SUPPORT_TIER2_MIN_SCORE`（默认 `0.2`）
- `RAG_NLTK_DATA_DIR`：systemd 下设 `/var/lib/private/wechat-relay/nltk_data`，确保 `nltk.pathsec` 在 `DynamicUser=yes` 下满足单硬链与属主一致。

---

## 已知语料缺口与待办（衔接第 4 步）

- 线下 `0.` 语料目前共 `5` 份文档、`66` 个块（已在 `2026-11-26` 真机测试命中 `exact_alias`、`decision_preset`、`tier2_clarify -> tier2_short`、`Feedback`、`Emergency`、`staff_case_appended` 与白名单实发）。
- 实测中「静音舱怎么预约？」因现有 `0.` 语料缺少空间/静音舱/座位预约说明而按设计触发 `tier2_fallback` 并建出 `case_id=4`；将在第 4 步前清理测试待办并补充对应 `0.` 公开资料后再重跑 `build-index`。

---

## 重建 `0.` 语料索引命令

```bash
rag-poc/.venv/bin/python -m support_backend build-index \
  --corpus rag-poc/corpus \
  --index-dir rag-poc/index_v2 \
  --env-file rag-poc/.env
```
