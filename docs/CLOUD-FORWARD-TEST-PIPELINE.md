# Step 1 · 微信云托管极简转发与自建服务器环境集中配置

## 目标

微信云托管仅作为入口消息转发器使用（`app.py`），所有回复逻辑、凭证、模型密钥与运行开关统一收敛在自建服务器环境变量文件 `/etc/wechat-relay.env` 中，使云托管侧仅需保留最少的转发配置。

## 首次实测记录（2026-11-26）

- 已完成 `微信云托管转发 -> https://wechat-dev.llink.cyou/relay/wechat -> /relay/send-test` 双向全链路真机验证，`custom/send` 直连回发返回 `errcode=0` 且手机确认收到回复。
- 已查明线上云托管真实推送头：`x-wx-source=other`、`x-wx-env=ai-customer-service-d9bn16017181`、`x-wx-appid` 与服务号 `WECHAT_APP_ID` 一致，因此 `WECHAT_CLOUD_EXPECTED_*` 可直接下沉到自建服务器校验。

## 环境变量职责划分

### 1. 微信云托管环境变量（仅保留 2 项）

`app.py` 在检测到 `WECHAT_FORWARD_URL` 且未设置 `WECHAT_TOKEN` 时，会自动采用纯转发默认配置（`WECHAT_CLOUD_PUSH_ENABLED=true`、`WECHAT_AUTO_REPLY_ENABLED=false`、`DASHBOARD_ENABLED=false`，不加载本地 RAG，也不暴露本地看板）：

```ini
WECHAT_FORWARD_URL=https://wechat-dev.llink.cyou/relay/wechat
WECHAT_FORWARD_TOKEN=<与自建服务器 RELAY_AUTH_TOKEN 一致>
```

> [!NOTE]
> 此前在云托管设置的 `WECHAT_CLOUD_PUSH_ENABLED`、`WECHAT_AUTO_REPLY_ENABLED`、`DASHBOARD_ENABLED`、`WECHAT_CLOUD_EXPECTED_SOURCE/ENV/APPID`、`WECHAT_TOKEN`、`WECHAT_APP_ID`、`WECHAT_APP_SECRET` 与 `RAG_*` 均可删除，无需继续维护在云托管控制台。默认转发超时 `3.0s`、默认 `WECHAT_FORWARD_FAIL_OPEN=true`。

### 2. 自建服务器环境变量（统一收敛到 `/etc/wechat-relay.env`）

`wechat-relay.service` 仅读取 `/etc/wechat-relay.env`（权限 `0600`，结构见 [wechat-relay.env.example](../deploy/wechat-relay.env.example)）；沙盒绑定与本地数据路径仍由 [wechat-relay.service](../wechat-relay.service) 固定提供：

- **转发鉴权与来源头校验**：`RELAY_AUTH_TOKEN`、`WECHAT_CLOUD_EXPECTED_SOURCE`、`WECHAT_CLOUD_EXPECTED_ENV`、`WECHAT_CLOUD_EXPECTED_APPID`（`relay_receiver.py` 在 `/relay/wechat` 校对被转发的 `wx_headers`，`CheckContainerPath` 探测不触发转发）。
- **微信直连回发凭证**：`WECHAT_APP_ID`、`WECHAT_APP_SECRET`。
- **Tier 1 / Tier 2 模型配置**：`SUPPORT_DECISION_BASE_URL`、`SUPPORT_DECISION_API_KEY`、`RAG_LLM_*`、`RAG_EMBEDDING_*`。
- **运行开关与测试白名单**：`SUPPORT_AUTO_REPLY_ENABLED`、`SUPPORT_AUTO_REPLY_ALLOWLIST`、`SUPPORT_MERGE_ENABLED`、`SUPPORT_IMMEDIATE_MAX_PREEMPTS`。

> [!TIP]
> 仓库根 `.env` 与 `rag-poc/.env` 仅保留作离线脚本（如 `rag-poc/.venv/bin/python -m support_backend build-index --env-file rag-poc/.env`）或紧急切回 `wechat-dev-test.service` 使用；线上主服务 `wechat-relay.service` 只认 `/etc/wechat-relay.env`。

## 验证与日常维护命令

1. **检查服务与状态**：
   ```bash
   systemctl reload-or-restart wechat-relay.service
   curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" https://wechat-dev.llink.cyou/relay/support/overview
   ```
2. **查看最近转发与出站测试回复**：
   ```bash
   curl -fsS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "https://wechat-dev.llink.cyou/relay/events?limit=5"
   curl -fsS -X POST -H "Authorization: Bearer $RELAY_AUTH_TOKEN" -H "Content-Type: application/json" \
     -d '{"seq":1,"text":"测试直连回发"}' https://wechat-dev.llink.cyou/relay/send-test
   ```
