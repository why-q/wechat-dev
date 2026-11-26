# Step 1 · 微信云托管消息转发与回发验证管道

## 目标

对应 [图书馆智能客服设计](LIBRARY-SUPPORT-DESIGN.md) 数据流图中的 `Message -> Wechat Cloud -> API Server` 及 [自动回复时序](LIBRARY-SUPPORT-DESIGN.md#自动回复时序)：
1. 验证微信云托管（GitHub + `Dockerfile`）转发消息至自有公网服务器（**已实测验证通过**）。
2. 将接收端切到 Nginx HTTPS + `systemd` 守护，并收紧云托管 `x-wx-*` 请求头校验。
3. 在同一管道上完成出站客服消息 `custom/send` 验证，核实 API 返回码与手机实际收件。

## 首次实测记录（2026-11-26）

从自有服务器本地采集到的 2 条真实转发事件确认：
- 传输格式为 `application/json`；云托管收到推送与自有服务器落地在同一秒内完成。
- `msg_type=event`（如菜单点击，含 `Event`、`EventKey`、`MenuId`）不含 `MsgId`；`msg_type=text` 包含 `MsgId`。
- 云托管附加请求头键为 `x-wx-appid`、`x-wx-env`、`x-wx-openid`、`x-wx-region`、`x-wx-service`、`x-wx-source`，其中 `x-wx-source` 实测值为 `other`。

## 架构与行为

1. **云托管端转发与来源校验（[app.py](../app.py)）**
   - 当 `WECHAT_FORWARD_URL` 为非空 `http://` 或 `https://` 地址时，`/wechat` 在跳过 `action="CheckContainerPath"` 检测后，按可选的 `WECHAT_CLOUD_EXPECTED_SOURCE`（推荐 `other`）、`WECHAT_CLOUD_EXPECTED_ENV`、`WECHAT_CLOUD_EXPECTED_APPID` 校验云内推送请求头（不匹配返回 `403`）。
   - 随后同步（worker 线程、`WECHAT_FORWARD_TIMEOUT_SECONDS` 默认 `3.0` 秒、上限 `4.5` 秒）向目标地址 `POST` JSON 信封；默认 `WECHAT_FORWARD_FAIL_OPEN=true`，避免转发异常导致正式服务号报错。
   - 若显式设置 `WECHAT_FORWARD_ALLOW_TEST_REPLY=true` 且接收端返回非重复事件的 `cloud_test_reply` 字段，则触发云端 `_send_customer_message`（可验证 `WECHAT_OPEN_API_ENABLED=true` 云调用发信），并在非零 `errcode` 时安全记录 `errmsg`。

2. **自有服务器接收与回发测试端（[relay_receiver.py](../relay_receiver.py)）**
   - 启动强制校验 `RELAY_AUTH_TOKEN` 非空，防止无鉴权暴露。
   - `GET /healthz`：无需鉴权，返回 `200 ok`。
   - `POST /relay/wechat`：校验 `Authorization: Bearer <RELAY_AUTH_TOKEN>`；按非空 `msg_id` 去重（重启后从 JSONL 恢复）；以目录 `0700`、文件 `0600` 持久化写入 `RELAY_LOG_PATH`（默认 `data/relay-events.jsonl`）。可选 `RELAY_DIRECT_ECHO_TEXT`（触发直连回发）或 `RELAY_CLOUD_ECHO_TEXT`（配合 `WECHAT_FORWARD_ALLOW_TEST_REPLY=true` 测试云调用回发）。
   - `GET /relay/events?limit=20`：需 Bearer 鉴权，倒序返回最近收到的事件。
   - `POST /relay/send-test`：需 Bearer 鉴权，传入 `{"seq": 2, "text": "..."}`（或 `to_user`），使用 `WECHAT_APP_ID` + `WECHAT_APP_SECRET` 直连 `https://api.weixin.qq.com/cgi-bin/message/custom/send`（含 `access_token` 缓存与 `40001` 单次刷新重试），返回 `errcode`、`errmsg` 与耗时。

3. **Nginx HTTPS 与 systemd（[deploy/nginx-wechat-dev-test.conf](../deploy/nginx-wechat-dev-test.conf) · [wechat-relay.service](../wechat-relay.service)）**
   - Nginx 在 `80` 与 `443 ssl`（使用 `/etc/ssl/nginx/cert.pem` 覆盖 `*.llink.cyou`）统一反代 `/healthz`、`/relay/wechat`、`/relay/events`、`/relay/send-test` 到 `127.0.0.1:8090`（同时兼容 Cloudflare Flexible 到源站 80 与 Full / Full (Strict) 到源站 443；生产建议在 Cloudflare 将 SSL/TLS 模式设为 Full (Strict) 以保证回源加密），并保留 `/wechat` 回滚入口。
   - `wechat-relay.service` 使用 `DynamicUser=yes`、`StateDirectory=wechat-relay`，读取 `.env` 与可选 `/etc/wechat-relay.env`（格式须为 `RELAY_AUTH_TOKEN=...` 键值对），将数据保存在 `/var/lib/private/wechat-relay/relay-events.jsonl`。

## 验证操作步骤

### 1. 服务器启用 HTTPS 与 systemd

1. 将共享 `RELAY_AUTH_TOKEN` 以 `KEY=VALUE` 格式写入权限为 `0600` 的 `/etc/wechat-relay.env`（不要写入 Git）：

```bash
sudo install -m 0600 /dev/null /etc/wechat-relay.env
printf 'RELAY_AUTH_TOKEN=%s\n' '<与云托管 WECHAT_FORWARD_TOKEN 相同的值>' | sudo tee /etc/wechat-relay.env >/dev/null
```
2. 安装单元与 Nginx 配置并重启：

```bash
sudo ln -sf /root/Work/wechat_dev_test/wechat-relay.service /etc/systemd/system/wechat-relay.service
sudo cp /root/Work/wechat_dev_test/deploy/nginx-wechat-dev-test.conf /etc/nginx/conf.d/wechat_dev_test.conf
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl daemon-reload && sudo systemctl enable --now wechat-relay.service
```

3. 验证公网 HTTPS：

```bash
curl --fail https://wechat-dev.llink.cyou/healthz
```

### 2. 更新云托管环境变量（紧校验 + HTTPS）

```text
WECHAT_CLOUD_PUSH_ENABLED=true
WECHAT_AUTO_REPLY_ENABLED=false
DASHBOARD_ENABLED=false
WECHAT_CLOUD_EXPECTED_SOURCE=other
WECHAT_CLOUD_EXPECTED_ENV=<你的云环境ID>
WECHAT_CLOUD_EXPECTED_APPID=<你的公众号AppID>
WECHAT_FORWARD_URL=https://wechat-dev.llink.cyou/relay/wechat
WECHAT_FORWARD_TOKEN=<与 RELAY_AUTH_TOKEN 相同>
WECHAT_FORWARD_TIMEOUT_SECONDS=3.0
WECHAT_FORWARD_FAIL_OPEN=true
```

### 3. 测试出站回发到手机

1. **方式 A（自有服务器直连 `custom/send`，优先）**：先用手机给公众号发一条消息（记下 `seq`），然后运行：

```bash
curl -sS -X POST https://wechat-dev.llink.cyou/relay/send-test \
  -H "Authorization: Bearer $RELAY_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"seq":1,"text":"测试自有服务器直连回复"}'
```

记录返回的 `errcode`、`errmsg` 并核对手机是否收到该消息。

2. **方式 B（云托管开放接口服务 `custom/send`，按需对照）**：在云托管临时加设 `WECHAT_OPEN_API_ENABLED=true`、`WECHAT_FORWARD_ALLOW_TEST_REPLY=true`（并确认云托管开放接口服务已将 `/cgi-bin/message/custom/send` 加入云调用白名单），在 `/etc/wechat-relay.env` 临时设置 `RELAY_CLOUD_ECHO_TEXT=测试云调用回复` 后重启 `wechat-relay.service`，手机发消息触发一次回发，查看云托管日志的 `errcode`/`errmsg` 与手机收件，测毕清除这两个临时开关。
