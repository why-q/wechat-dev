# Step 1 · 微信云托管极简转发与自建服务器环境集中配置

## 目标

微信云托管仅作为入口消息转发器使用（`app.py`），所有回复逻辑、凭证、模型密钥、来源校验与运行开关统一收敛在自建服务器环境变量文件 `/etc/wechat-relay.env` 中，使云托管侧仅需保留 `2` 项转发配置。

## 首次实测与历史校验记录

- **2026-11-26（首次打通）**：完成 `微信云托管转发 -> https://wechat-dev.llink.cyou/relay/wechat -> /relay/send-test` 双向全链路真机验证，`custom/send` 直连回发返回 `errcode=0` 且手机确认收到回复。
- **2026-11-27（296 条回调核对）**：对 `/var/lib/private/wechat-relay/relay-events.jsonl` 历史 `296` 条回调完成全量核对，全部均为 `transport=cloud`、`x-wx-source=other`、`x-wx-env=ai-customer-service-d9bn16017181` 且 `x-wx-appid` 与 `WECHAT_APP_ID` 一致，确认可将 `WECHAT_CLOUD_EXPECTED_*` 下沉到自建服务器校验；精简后已完成线上真机转发验证。

## 环境变量职责划分

### 1. 微信云托管环境变量（仅保留 2 项）

`app.py` 在检测到 `WECHAT_FORWARD_URL` 且未设置 `WECHAT_TOKEN` 时，会自动推导纯转发默认配置（`WECHAT_CLOUD_PUSH_ENABLED=true`、`WECHAT_AUTO_REPLY_ENABLED=false`、`DASHBOARD_ENABLED=false`，不加载本地 RAG，也不暴露本地看板）：

```ini
WECHAT_FORWARD_URL=https://wechat-dev.llink.cyou/relay/wechat
WECHAT_FORWARD_TOKEN=<与自建服务器 RELAY_AUTH_TOKEN 一致>
```

> [!NOTE]
> 先确保微信云托管已基于提交 `97f958c` 及之后代码完成发布，再清理控制台历史多余变量（`WECHAT_CLOUD_PUSH_ENABLED`、`WECHAT_AUTO_REPLY_ENABLED`、`DASHBOARD_ENABLED`、`WECHAT_CLOUD_EXPECTED_*`、`WECHAT_TOKEN`、`WECHAT_APP_ID`、`WECHAT_APP_SECRET`、`RAG_*` 等）。默认转发超时 `3.0s`、默认 `WECHAT_FORWARD_FAIL_OPEN=true`。

### 2. 自建服务器环境变量（统一收敛到 `/etc/wechat-relay.env`）

`wechat-relay.service` 仅读取 `/etc/wechat-relay.env`（权限 `0600`，模板见 [wechat-relay.env.example](../deploy/wechat-relay.env.example)）；沙盒绑定与本地数据路径由 [wechat-relay.service](../wechat-relay.service) 提供：

- **转发鉴权与来源头校验**：`RELAY_AUTH_TOKEN`、`WECHAT_CLOUD_EXPECTED_SOURCE`、`WECHAT_CLOUD_EXPECTED_ENV`、`WECHAT_CLOUD_EXPECTED_APPID`（`relay_receiver.py` 在 `/relay/wechat` 核对 `wx_headers`，不匹配返回 `403`；云托管 `CheckContainerPath` 在边缘直接返回 `success`，不触发转发）。
- **微信直连回发凭证**：`WECHAT_APP_ID`、`WECHAT_APP_SECRET`。
- **Tier 1 / Tier 2 模型配置**：`SUPPORT_DECISION_BASE_URL`、`SUPPORT_DECISION_API_KEY`、`RAG_LLM_*`、`RAG_EMBEDDING_*`。
- **运行开关与测试白名单**：`SUPPORT_AUTO_REPLY_ENABLED`、`SUPPORT_AUTO_REPLY_ALLOWLIST`、`SUPPORT_MERGE_ENABLED`、`SUPPORT_IMMEDIATE_MAX_PREEMPTS`。

> [!TIP]
> 仓库根 `.env` 与 `rag-poc/.env` 仅保留作离线脚本（如 `rag-poc/.venv/bin/python -m support_backend build-index --env-file rag-poc/.env`）或回滚至 `wechat-dev-test.service` 时使用；线上主服务 `wechat-relay.service` 只认 `/etc/wechat-relay.env`。

## 服务器路由、存储与日常检查

- **Nginx 反代配置**（[deploy/nginx-wechat-dev-test.conf](../deploy/nginx-wechat-dev-test.conf)）：公开 HTTPS `wechat-dev.llink.cyou` 反代 `/healthz`、`/relay/wechat`、`/relay/events`、`/relay/send-test` 及 `/relay/support/` 到 `127.0.0.1:8090`；回滚入口 `location = /wechat` 保留至 `127.0.0.1:8000`。
- **持久化文件位置**：`/var/lib/private/wechat-relay/relay-events.jsonl`（原始转发备份，`0600`）与 `/var/lib/private/wechat-relay/library-support.db`（SQLite WAL，`0600`）。

```bash
# 1. 重载并核验 systemd 服务状态
sudo systemctl daemon-reload
sudo systemctl restart wechat-relay.service
systemctl is-active wechat-relay.service

# 2. 查看运营总览与最近路由决策（需在受控终端加载 RELAY_AUTH_TOKEN）
curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" https://wechat-dev.llink.cyou/relay/support/overview
curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "https://wechat-dev.llink.cyou/relay/support/triage-runs?limit=5"

# 3. 查看原始转发并测试直连 custom/send
curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "https://wechat-dev.llink.cyou/relay/events?limit=5"
curl -fsS -X POST -H "Authorization: Bearer $RELAY_AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"seq":1,"text":"测试直连回发"}' https://wechat-dev.llink.cyou/relay/send-test
```
