# 微信公众号测试号回调服务

服务已部署在本机 `127.0.0.1:8000`，由 `wechat-dev-test.service` 守护并开机启动。Nginx、DNS、证书和微信后台配置未由本次修改；公网路由保持不变。

本轮支持测试号明文 XML 回调：GET 保留原有签名验证并原样返回 `echostr`；合法文字 POST 立即返回纯文本 `success`，随后等待 10 秒，再通过 `sendcustommessage` 客服消息 API 尝试发送固定回复“已收到你的消息，测试号自动回复演示已接通。”。其他类型和事件仍返回 `success`。回复仅为演示，不是 AI 答案；不调用 AI 或转人工。

## 查看本地消息看板

浏览器打开 `http://127.0.0.1:8000/`，查看最近收到的文字、固定回复和发送状态。回调确认后 10 秒才尝试发消息；看板每 2 秒刷新，并在 API 返回 `errcode=0` 后显示“接口已接受”，其他结果显示失败。API 接受不等于手机一定送达。看板最多保留 100 条在内存中，服务重启后清空；正文不会写入日志或磁盘，记录不含 OpenID 或其他读者标识。

看板和 `/messages` JSON 接口没有登录认证，属于**不安全的本地演示**；仅供受信任的本机用户使用。服务绑定 `127.0.0.1`，不要通过 Nginx 或其他方式将 `/`、`/messages` 或整个应用公开到互联网。只反向代理 `/wechat`。

从自己的工作站远程查看时，使用 SSH 本地端口转发（保持 SSH 连接开启）：

```bash
ssh -L 8000:127.0.0.1:8000 <server-user>@<server-host>
```

然后在工作站浏览器打开 `http://127.0.0.1:8000/`。命令左侧第一个 `8000` 是工作站本地端口；若本地 8000 已占用，可改为 8001，例如 `ssh -L 8001:127.0.0.1:8000 <server-user>@<server-host>`，并打开 `http://127.0.0.1:8001/`。

## 在微信后台填写什么

| 项目 | 当前填写方式 |
|---|---|
| URL | 你通过 Nginx 提供的公网地址，路径为 `/wechat` |
| Token | 当前目录 `.env` 文件中 `WECHAT_TOKEN=` 后面的完整值 |
| JS 接口安全域名 | 本轮不用，可留空 |
| 用户列表 | 用自己的微信扫描测试号关注二维码，关注后查看 |
| 模板消息接口 | 本轮不用，不添加模板 |
| AppID、AppSecret | 由服务从 `.env` 读取 `WECHAT_APP_ID`、`WECHAT_APP_SECRET`，仅用于获取 `access_token` 和发送客服消息；不要提供给聊天或前端 |

Token 已在服务器上随机生成，文件权限为 `0600`，没有写入源码或聊天。你可以在当前目录的私人 SSH 终端查看：

```bash
grep '^WECHAT_TOKEN=' .env
```

只复制等号后面的值，不复制 `WECHAT_TOKEN=` 前缀，不带空格或引号。不要将输出发到聊天、提交到版本库或贴进截图。`.env` 现在还含 AppSecret；不要用 `cat .env` 显示全部凭据。Token 与 AppSecret、access_token 不同。

## 配置 Nginx

在已有、证书有效的 HTTPS `server` 中添加下面的 location，按你自己的域名配置 DNS 和证书：

```nginx
location = /wechat {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 3s;
    proxy_read_timeout 5s;
    proxy_send_timeout 5s;
    client_max_body_size 256k;
    access_log off;
}
```

这是 location 片段，不是完整的 HTTPS 配置。`proxy_pass` 的端口后不加 `/`，以保留 `/wechat` 和验签查询参数。只关闭此回调路径的访问日志，避免把签名参数记入 Nginx 日志。**此服务仅允许精确反代 `/wechat`；不要添加根路径或 `/messages` 的代理，也不要把整个应用公开。**

微信的公网入口使用 HTTP 80 或 HTTPS 443，不要填写本地地址或公网 `:8000`。本机 8000 不需要向公网开放。如果使用 Cloudflare 代理，回调地址不能出现登录、验证码或 JavaScript 挑战页，也不能缓存回调响应。

比如公网入口为 `https://wx.example.com`，微信 URL 就填 `https://wx.example.com/wechat`。这是示例，需换成你的真实域名。

## 验证顺序

1. 本机检查健康状态和看板：

   ```bash
   curl --fail http://127.0.0.1:8000/healthz
   ```

   预期响应为 `ok`。在本机浏览器打开 `http://127.0.0.1:8000/`；远程工作站通过上面的 SSH 隧道查看。直接访问 `/wechat` 没有签名，会返回 HTTP 403，这是预期行为。
2. 完成 Nginx 配置后，只通过公网 `/wechat` 检查链路可达；无签名时预期 HTTP 403，而不是代理错误或挑战页。这不代表验签或真实消息收发通过。
3. 在微信测试号页面填写公网 `/wechat` URL 和同一个 Token，提交接口配置。只有微信后台显示成功，才证明实际微信 GET 验证通过。
4. 新版真实手机送达尚未验证。接下来由用户从手机发送一条文字，观察本地看板是否在 10 秒后更新发送状态，并核对手机是否收到消息；不要用合成读者标识对真实微信 API 发请求。

### 微信后台消息入口的边界

官方[消息与事件推送文档](https://developers.weixin.qq.com/doc/subscription/guide/dev/push/)说明，用户消息会转发到配置的开发者 URL，启用后网页中设置的自动回复和自定义菜单失效；文档没有说明普通公众平台“消息管理”是否仍保留消息副本，不能据此断言会显示或不会显示。

用户确认，工作人员使用微信公众平台自带网页后台处理私信，但未确认具体入口是普通“消息管理”还是公众号“客服功能”。两者是不同入口，不能把其中任何一个当作已确认的实际页面。公众号[客服功能](https://developers.weixin.qq.com/doc/subscription/guide/product/kf/intro.html)是独立的网页客服工作台；当前 demo 没有返回 `MsgType=transfer_customer_service`，不会转入该工作台。独立产品“微信客服”是另一套产品，本项目尚未接入。

官方[获取聊天记录接口](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_getmsglist)用于查询客服聊天记录，单次查询时间跨度不超过 24 小时，每次最多 10,000 条。公众号和服务号的接口适用范围均为“仅认证”。这些是 `getmsglist` 查询 API 的限制，不代表整个客服功能的开通要求，也不代表聊天记录保存时长；官方开发者文档未给出记录保存期限。

官方[发送客服消息 API 说明](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_sendcustommessage)规定，用户发送消息场景的客服消息额度最多 5 条，有效期为 48 小时；这是发送额度及有效期，不是聊天记录保存期限。

部署后先由用户手机发送一条文字，核对本地看板状态及手机是否收到新回复；之后再只读查看测试号是否有普通消息管理页或公众号客服功能入口，以及该消息是否出现在对应官方后台。工作人员的确切入口和回调消息是否显示在普通消息管理中仍未确认；若测试号没有入口或官方后台没有显示消息，不能据此推断机构正式号行为。

手机未收到回复时，先看本地看板发送状态：API 返回 `errcode=0` 表示接口接受请求，不保证微信手机送达；失败状态不自动重试。不要据此宣称 AI 已接入。

## 接口与边界

| 接口或请求 | 结果 |
|---|---|
| `GET /healthz` | HTTP 200，纯文本 `ok` |
| `GET /` | 本地消息看板 HTML；未做身份认证 |
| `GET /messages` | 看板 JSON：时间、消息正文、固定回复与发送状态；不含读者标识 |
| 合法签名的明文文字 `POST /wechat` | HTTP 200，纯文本 `success`；不等待出站 API |
| 文字回调确认后 | 后台等待 10 秒，再以 `sendcustommessage` 的 JSON 格式发送固定文字；检查响应 `errcode == 0` 并更新看板状态 |
| 合法签名的明文非文字消息或事件 `POST /wechat` | HTTP 200，纯文本 `success`，不发客服消息 |
| 缺失或错误签名 | HTTP 403 |
| 畸形 XML、DTD、自定义实体、错误根节点或加密消息 | HTTP 400 |
| 超过 256 KiB 的消息体 | HTTP 413，包括分块传输 |

仅支持明文模式，不支持 AES 安全模式或兼容模式。相同 `MsgId` 的文字回调重试仍返回 `success`；若该 ID 仍在最近 100 条记录中，只保留一个看板条目并只安排一次出站发送，避免重复回复。记录淘汰后不再去重。若首次出站失败，重复回调不触发重试；没有 `MsgId` 时，每次回调单独处理。后台任务和记录都只在内存中；进程若在确认回调后、出站请求前退出，回复可能丢失，服务重启后看板清空。没有实现发送重试或持久队列。

出站客服消息使用 `.env` 的 AppID/AppSecret 获取并缓存 `access_token`，API 调用最长等待 5 秒；不记录凭据、OpenID 或正文。依官方规则，用户发送消息场景最多发送 5 条客服消息，有效期 48 小时；当前每条新文字回调最多尝试发送一条，配额耗尽或接口失败时看板显示失败。`errcode=0` 只表示接口接受请求，不保证用户手机实际收到。

应用关闭 HTTP 访问日志，只记录已接受消息类型及出站 API 结果码，不记录完整 URL、签名、OpenID、MsgId 或正文。看板最多保留 100 条文字记录于进程内存，服务重启后清空，不保存用户标识。

## 管理与更新

```bash
systemctl status wechat-dev-test.service
systemctl restart wechat-dev-test.service
systemctl stop wechat-dev-test.service
```

停止且取消开机启动：

```bash
systemctl disable --now wechat-dev-test.service
```

依赖记录在 `pyproject.toml` 和 `uv.lock`，虚拟环境位于 `.venv`。在当前目录安装锁定版本：

```bash
uv sync --locked --python /usr/bin/python3 --no-dev
```

更新应用、依赖或 Token 后重启服务。修改 service 文件时，先运行 `systemctl daemon-reload`，再重启。

systemd 使用动态非特权 UID，仅将 `app.py` 和 `.venv` 只读挂载到服务自己的 `/srv/wechat-dev-test`；这里是运行时视图，不是第二份代码。聊天导出和背景文档不会挂入应用目录。systemd 从主机侧 `EnvironmentFile=/root/Work/wechat_dev_test/.env` 加载 `WECHAT_TOKEN`、`WECHAT_APP_ID`、`WECHAT_APP_SECRET`，仅将它们注入服务环境；应用不会读取或暴露 `.env` 文件内容。

service 文件通过符号链接安装，代码、虚拟环境、`.env` 仍在当前项目目录。移动目录前，需同步修改 service 中的源路径并重新安装链接。

## 已完成的本地验证

`.venv/bin/python -m unittest discover -s tests -v` 的 8 项测试全部通过：

- `test_api_json_request_uses_post_and_utf8_json`
- `test_customer_message_requires_errcode_zero`
- `test_customer_message_uses_official_json_and_caches_access_token`
- `test_get_signature_echo_semantics_remain_unchanged`
- `test_invalid_signature_is_rejected`
- `test_non_text_event_returns_success_without_sending`
- `test_recent_message_store_is_bounded_to_one_hundred`
- `test_text_callback_acknowledges_then_sends_and_deduplicates`

签名 XML 文字回调测试返回 HTTP 200 `success`，并断言 10 秒等待任务只在响应 body 已发送后运行；重复 `MsgId` 仍被确认，但只保留一条记录、只安排一次发送。API 测试用 mock 验证 POST JSON、token 缓存与 `errcode` 判断，不会向微信发送请求。

已受控重启现有 systemd 服务。实际 HTTP smoke：服务为 active，端口仅监听 `127.0.0.1:8000`；`/healthz` 返回 `ok`，`/` 返回 `200 text/html`，重启清空后的 `/messages` 返回 `[]`。另向运行中的服务发送了不含读者标识的本机签名 event XML POST，得到 HTTP 200 `success`，看板仍为空；未向实时微信 API 发送合成文字消息。新版客服消息的真实手机送达仍待用户触发验证。
官方参考：[消息与事件推送](https://developers.weixin.qq.com/doc/subscription/guide/dev/push/)、[发送客服消息](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_sendcustommessage)、[获取 access_token](https://developers.weixin.qq.com/doc/subscription/api/base/api_getaccesstoken)。
