# Step 1 · 微信云托管消息转发测试管道

## 目标

对应 [图书馆智能客服设计](LIBRARY-SUPPORT-DESIGN.md) 数据流图中的 `Message -> Wechat Cloud -> API Server` 第一步：验证微信云托管（通过 GitHub + `Dockerfile` 部署）在收到公众号消息推送后，能否主动通过 HTTP/HTTPS 将消息实时转发到自有公网服务器。

## 架构与行为

1. **云托管端转发（[app.py](../app.py)）**
   - 当 `WECHAT_FORWARD_URL` 为非空 `http://` 或 `https://` 地址时，`/wechat` 在完成来源/签名校验并跳过 `action="CheckContainerPath"` 检测请求后，同步（在 worker 线程中执行、受 `WECHAT_FORWARD_TIMEOUT_SECONDS` 默认 `3.0` 秒限制）向目标地址 `POST` JSON 信封：
     - `schema_version` (`1`), `transport` (`cloud` / `signed`), `received_at_utc`, `content_type`
     - `msg_type`, `msg_id`, `from_user`, `to_user`, `create_time`, `content`, `event`
     - `wx_headers`（所有 `x-wx-*` 请求头，便于核对生产 `x-wx-source` 等头信息）
     - `raw_body`（原始回调报文字符串）
   - 如设置 `WECHAT_FORWARD_TOKEN`，请求携带 `Authorization: Bearer <token>`。
   - 默认 `WECHAT_FORWARD_FAIL_OPEN=true`：即使自有服务器不可达或超时，云托管日志记录 `WeChat callback forward failed`，仍向微信返回 `200 success`，避免正式服务号向读者提示故障或触发微信重复重试；若需严格失败透传可设 `WECHAT_FORWARD_FAIL_OPEN=false`。
   - 转发逻辑位于 `if not _AUTO_REPLY_ENABLED` 返回之前，因此可设置 `WECHAT_AUTO_REPLY_ENABLED=false` 关闭 RAG 初始化与自动回复，单独验证消息接收与转发。
   - 云托管标准日志不输出 `WECHAT_FORWARD_URL`、Token、OpenID、`MsgId` 或消息正文。

2. **自有服务器接收端（[relay_receiver.py](../relay_receiver.py)）**
   - 启动强制校验 `RELAY_AUTH_TOKEN` 非空，防止公网端口无鉴权暴露。
   - `GET /healthz`：无需鉴权，返回 `200 ok`，用于外部连通性检查。
   - `POST /relay/wechat`：校验 `Authorization: Bearer <RELAY_AUTH_TOKEN>`；按非空 `msg_id` 去重（重启后从 JSONL 恢复）；以目录 `0700`、文件 `0600` 持久化写入 `RELAY_LOG_PATH`（默认 `data/relay-events.jsonl`，已在 `.gitignore` 中忽略）。
   - `GET /relay/events?limit=20`：需携带 `Authorization: Bearer <RELAY_AUTH_TOKEN>`，按时间倒序返回最近收到的消息（上限 100 条）。

## 验证操作步骤

### 1. 在自有服务器启动接收端

在仓库根目录启动接收服务（示例端口 `8090`，需在云服务器安全组/防火墙放行对应端口，或使用 Nginx HTTPS 反代）：

```bash
export RELAY_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export RELAY_LOG_PATH="/root/Work/wechat_dev_test/data/relay-events.jsonl"
./.venv/bin/python -m uvicorn relay_receiver:app --host 0.0.0.0 --port 8090 --no-access-log --no-server-header
```

在另一台机器测试公网连通性（应返回 `ok`）：

```bash
curl --fail http://<PUBLIC_IP>:8090/healthz
```

### 2. 配置微信云托管环境变量并发布

将包含本次修改的提交推送到 GitHub `main` 分支后，在微信云托管控制台配置服务环境变量（请勿把 Token 或 URL 写入代码或镜像）：

```text
WECHAT_CLOUD_PUSH_ENABLED=true
WECHAT_AUTO_REPLY_ENABLED=false
DASHBOARD_ENABLED=false
WECHAT_FORWARD_URL=http://<PUBLIC_IP>:8090/relay/wechat
WECHAT_FORWARD_TOKEN=<与 RELAY_AUTH_TOKEN 相同的值>
WECHAT_FORWARD_TIMEOUT_SECONDS=3.0
WECHAT_FORWARD_FAIL_OPEN=true
```

### 3. 真实验收

1. 从授权微信账号向公众号发送一条文字消息。
2. 检查云托管服务日志：确认出现 `WeChat callback forward started` 与 `WeChat callback forward succeeded: status=200 duration_ms=...`。
3. 检查自有服务器 `data/relay-events.jsonl` 或调用 `GET /relay/events`：

```bash
curl -sS -H "Authorization: Bearer $RELAY_AUTH_TOKEN" "http://127.0.0.1:8090/relay/events?limit=5"
```

4. 核对收到的记录中的 `content`、`from_user`、`msg_id`、`wx_headers`（可确认真实 `x-wx-source` 值）与 `raw_body`。
5. 正式接入读者消息前，建议通过 Nginx 为接收端配置 HTTPS 证书。
