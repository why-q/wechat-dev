# Step 1：微信云托管极简转发与自建服务器环境集中配置

## 目标

微信云托管（`app.py`）只保留入口消息转发，业务处理、来源校验、模型密钥、微信凭证和运行开关统一放在自建服务器 `/etc/wechat-relay.env`（服务单元 `wechat-relay.service`），云托管端只配置 `2` 项转发变量。

## 已验证状态

- 双向链路「公众号私信 -> 云托管 `/wechat` -> 自建后端 `/relay/wechat` -> 直连 `custom/send` 手机收件」已实机跑通（`errcode=0`）。
- 云托管回调携带 `transport=cloud` 及 `x-wx-source` / `x-wx-env` / `x-wx-appid` 请求头，来源头校验已迁至自建端 `relay_receiver.py`，云托管控制台仅留 `2` 项变量后实测转发正常。

## 环境变量职责划分

### 1. 微信云托管（仅保留 2 项）

`app.py` 检测到 `WECHAT_FORWARD_URL` 且未设置 `WECHAT_TOKEN` 时，自动进入纯转发模式（`WECHAT_CLOUD_PUSH_ENABLED=true`、`WECHAT_AUTO_REPLY_ENABLED=false`、`DASHBOARD_ENABLED=false`），默认转发超时 `3.0s`（上限 `4.5s`）、`WECHAT_FORWARD_FAIL_OPEN=true`，不加载本地 RAG 也不暴露看板；`CheckContainerPath` 探测在云托管侧直接返回 `success`。

```ini
WECHAT_FORWARD_URL=https://wechat-dev.llink.cyou/relay/wechat
WECHAT_FORWARD_TOKEN=<与自建服务器 RELAY_AUTH_TOKEN 一致>
```

云托管控制台无需保留 `WECHAT_TOKEN`、`WECHAT_APP_ID`、`WECHAT_APP_SECRET`、`WECHAT_CLOUD_PUSH_ENABLED`、`WECHAT_AUTO_REPLY_ENABLED`、`DASHBOARD_ENABLED`、`WECHAT_CLOUD_EXPECTED_*` 或 `RAG_*`（需运行提交 `97f958c` 及之后代码）。

### 2. 自建服务器（统一配置 `/etc/wechat-relay.env`）

[wechat-relay.service](../wechat-relay.service) 使用 `DynamicUser=yes` 与只读挂载，只读取 `/etc/wechat-relay.env`（权限 `0600`，模板见 [wechat-relay.env.example](../deploy/wechat-relay.env.example)）：

- **转发鉴权与云来源校验**：`RELAY_AUTH_TOKEN`、`WECHAT_CLOUD_EXPECTED_SOURCE`、`WECHAT_CLOUD_EXPECTED_ENV`、`WECHAT_CLOUD_EXPECTED_APPID`（`relay_receiver.py` 在 `/relay/wechat` 核对 `wx_headers`，不匹配返回 `403`）。
- **微信直连回发凭证**：`WECHAT_APP_ID`、`WECHAT_APP_SECRET`。
- **Tier 1 / Tier 2 模型配置**：`SUPPORT_DECISION_BASE_URL`、`SUPPORT_DECISION_API_KEY`、`RAG_LLM_*`、`RAG_EMBEDDING_*`。
- **运行开关与灰度白名单**：`SUPPORT_AUTO_REPLY_ENABLED`、`SUPPORT_AUTO_REPLY_ALLOWLIST`、`SUPPORT_MERGE_ENABLED`、`SUPPORT_IMMEDIATE_MAX_PREEMPTS`。

> [!TIP]
> 仓库根 `.env` 与 `rag-poc/.env` 仅用于离线脚本（如 `rag-poc/.venv/bin/python -m support_backend build-index --env-file rag-poc/.env`）或应急回滚 `wechat-dev-test.service`；线上主服务 `wechat-relay.service` 只认 `/etc/wechat-relay.env`。

## 服务器路由、存储与日常检查

- **Nginx 反代**（[nginx-wechat-dev-test.conf](../deploy/nginx-wechat-dev-test.conf)）：`wechat-dev.llink.cyou` 将 `/healthz`、`/relay/wechat`、`/relay/events`、`/relay/send-test` 与 `/relay/support/` 反代到 `127.0.0.1:8090`；回滚入口 `location = /wechat` 保留至 `127.0.0.1:8000`（`wechat-dev-test.service`）。
- **持久化文件**：`/var/lib/private/wechat-relay/relay-events.jsonl`（原始转发备份，`0600`）与 `/var/lib/private/wechat-relay/library-support.db`（SQLite WAL，`0600`）。

```bash
# 1. 重启或检查服务状态（修改 unit 文件后需先执行 sudo systemctl daemon-reload）
sudo systemctl restart wechat-relay.service
systemctl is-active wechat-relay.service

# 2. 查看运营总览、最近分诊决策与原始转发（需先在受控终端加载 RELAY_AUTH_TOKEN）
curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "https://wechat-dev.llink.cyou/relay/support/overview"
curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "https://wechat-dev.llink.cyou/relay/support/triage-runs?limit=5"
curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "https://wechat-dev.llink.cyou/relay/events?limit=5"

# 3. 按需手动测试直连 custom/send（务必指定测试白名单 OpenID，避免误发线上读者）
curl -fsS -X POST -H "Authorization: Bearer $RELAY_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"to_user":"<测试白名单OpenID>","text":"测试直连回发"}' "https://wechat-dev.llink.cyou/relay/send-test"
```
