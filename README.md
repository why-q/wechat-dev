# 微信公众号回调与馆藏 RAG

RAG 版本已通过 `systemctl daemon-reload` 和服务重启部署。用户最近运行重启脚本，报告 `service=active`、RAG 初始化完成及 `/healthz` 检查通过。最近一次直接调用微信 access-token API 成功，返回有效期 7200 秒；这证明当时 AppID/AppSecret 和 API 调用来源可获取 access_token，不代表客服消息已送达。用户报告已轮换公众号 AppSecret；`WECHAT_TOKEN` 是否轮换或与后台一致尚未确认。用户浏览器访问公网 `/wechat` 得到 `invalid signature`，与请求到达应用但未通过签名校验相符，不代表微信后台验证成功。公网 `/health` 返回 404；站点只反代 `/wechat`，应用健康端点是本机 `/healthz`，不应公开代理。用户确认当前是无实际用途的测试公众号，并授权继续测试。

RAG 会将读者问题正文及命中的馆藏片段发送至 `rag-poc/.env` 配置的远程 embedding/chat provider；OpenID 仅传给微信客服消息 API。用户已确认数据处理授权及 provider 数据区域/留存条款。provider 合成连通性探针通过。一次本机签名合成文字回调获得 `success`，并生成带来源编号的回答；但返回摘录未清楚支持答案，引用相关性尚未证明。此前合成 OpenID 的客服消息尝试返回 `errcode=48001`；之后一次 access-token API 探针成功，但实际手机送达尚未确认。答案与拒答能力尚未系统评测，检索阈值 `0.2` 未校准。

## 微信云托管迁移

代码侧已经增加微信云托管入口，相关文件为 `Dockerfile` 和 `.dockerignore`。镜像内固定使用 `0.0.0.0:${PORT}`，RAG 包通过 `PYTHONPATH=/app/rag-poc` 加载，索引和 4 份 Markdown 语料作为镜像只读内容发布。

本轮已确定使用云托管控制台提供的**默认公网域名**，不绑定 `llink.cyou`，也不配置 CNAME 或自定义证书。公众号服务器 URL 使用该域名的 `/wechat` 路径。因为这条路线必须从公网域名进入，入站回调采用原有 `signature` + XML 分支；不启用云托管「消息推送」，否则微信会覆盖这个服务器 URL，域名就不再是实际入口。

### 云托管协议差异

云托管「消息推送」不是普通公众号服务器回调：云托管推送不带 `signature`，而是使用带 `x-wx-source` 的 JSON 或 XML 明文请求。`/wechat` 在 `WECHAT_CLOUD_PUSH_ENABLED=true` 且存在该请求头时走此分支；普通签名 GET/POST 分支仍保留用于本机和旧服务器回滚。

云托管检测请求也走 `/wechat`：

```json
{"action":"CheckContainerPath"}
```

应用对该请求返回 HTTP 200 和 `success`。配置消息推送时填写路径 `/wechat`，格式建议选 JSON。云托管消息推送开启后，微信会覆盖原公众号服务器 URL；解绑后不会自动恢复旧 URL，必须按微信后台要求先停用再重新启用原服务器配置。

客服消息在云端使用微信云托管「开放接口服务」：设置 `WECHAT_OPEN_API_ENABLED=true` 后，应用向 `http://api.weixin.qq.com/cgi-bin/message/custom/send` 发送 JSON，不再获取或拼接 `access_token`。云托管控制台必须开启开放接口服务，并将 `/cgi-bin/message/custom/send` 加入云调用白名单。不要在云端关闭该开关再直连微信 API：云托管没有固定公网出口 IP，直连方案无法依赖公网 IP 白名单。

开放接口服务只改变云调用鉴权和路由，不自动授予公众号客服消息权限。此项目此前对同一账号的直连 `custom/send` 曾收到 `errcode=48001`，因此云端客服发送目前只能视为**未完成真实验证**；若云调用仍返回 `48001`，需要在账号侧开通相应客服消息能力，或改用公众号被动回复。被动回复必须在同一个消息推送响应中完成并受约 5 秒限制，当前远程 RAG 耗时不适合直接改成被动 RAG 回复。

云托管消息推送应关闭服务公网访问，让微信通过云内链路投递。`x-wx-source` 是微信侧请求来源标记，不是密码或签名；如果服务开启公网访问，外部请求可以伪造该请求头并跳过旧 signature 分支。因此 `WECHAT_CLOUD_PUSH_ENABLED` 默认关闭，只有在控制台关闭公网访问、明确使用云托管消息推送时才开启；若必须公开服务，应关闭该无签名分支并使用旧签名入口或增加受信任网关。

### 云端环境变量

云托管控制台的服务环境变量至少设置：

```text
WECHAT_TOKEN=<与公众号后台服务器配置相同的 Token>
WECHAT_CLOUD_PUSH_ENABLED=false
WECHAT_OPEN_API_ENABLED=true
DASHBOARD_ENABLED=false

RAG_LLM_API_BASE=...
RAG_LLM_API_KEY=...
RAG_LLM_MODEL=...
RAG_EMBEDDING_API_BASE=...
RAG_EMBEDDING_API_KEY=...
RAG_EMBEDDING_MODEL=...
```

当前默认域名路线仍需要 `WECHAT_TOKEN` 做入站 signature 校验；`WECHAT_APP_ID` 和 `WECHAT_APP_SECRET` 在开放接口服务模式下不用于客服消息鉴权。所有 token 和 provider key 必须只放在云托管环境变量，不要放入镜像、Dockerfile 或代码包。`WECHAT_CLOUD_PUSH_ENABLED=true` 仅供关闭公网访问的云内消息推送备选路线，不是本轮配置。

### 部署前检查

本地构建和运行：

```bash
docker build -t wechat-rag:local .
docker run --rm -p 8080:80 \
  -e WECHAT_TOKEN=local-smoke-token \
  -e WECHAT_CLOUD_PUSH_ENABLED=false \
  -e WECHAT_OPEN_API_ENABLED=true \
  -e DASHBOARD_ENABLED=false \
  wechat-rag:local
```

随后检查：

```bash
curl --fail http://127.0.0.1:8080/healthz
./.venv/bin/python -m unittest discover -s tests -v
```

本轮选定的默认域名路线使用签名 XML；`CheckContainerPath` 和 `x-wx-source` JSON 测试仅用于验证未启用的云内消息推送备选分支。

手动上传代码包的官方大小上限为 2 MiB；如果代码包超过上限，应改用 Git 仓库流水线或镜像仓库。`rag-poc/index/` 已取消 Git 忽略并必须随 GitHub 构建上下文提交；语料或索引更新需重新提交并发布。

本次镜像 smoke 还使用占位 provider endpoint 实例化了 `RagResponder()`，输出 `RAG_RESPONDER_INIT_COMPLETE`；这证明镜像索引可加载，不代表真实 provider 网络或客服 API 权限已验证。

### 使用 GitHub 绑定构建

推送前先检查 `git status` 和 staged diff，只加入需要部署的源码、`Dockerfile`、`.dockerignore`、`rag-poc/pyproject.toml`、`rag-poc/uv.lock`、语料及 `rag-poc/index/`；不要加入任何 `.env`、API key、微信凭据、`.venv/`、`.cache/` 或本机配置。索引约 1.24 MB，已从两层 `.gitignore` 中取消忽略，以便 GitHub 构建上下文包含它；索引/语料更新需随代码重新提交。

仓库可见性建议选择 **Private**。当前历史审计未发现 `.env` 或 `rag-poc/.env` 曾被提交，但 README、systemd 配置、服务器路径、域名和馆藏语料仍属于不必默认公开的部署信息；若确实要设为 Public，应先单独复核语料授权、基础设施细节和全部提交内容。

在微信云托管控制台选择「绑定 GitHub 仓库」时，授权 GitHub 应用仅访问此仓库，选定部署分支，并把 Dockerfile 路径设为仓库根目录 `Dockerfile`、构建上下文设为仓库根目录、服务端口设为 `80`。确认构建日志实际包含 `rag-poc/index` 且镜像构建成功后，再发布服务版本。不要把 `.env` 上传到 GitHub；provider keys、`WECHAT_TOKEN` 等只通过云托管环境变量设置。

GitHub 绑定只负责取代码和构建，不会替代之前列出的环境变量、最小副本、开放接口服务、云调用白名单或公众号后台 URL 配置。

### 副本数与异步任务硬约束

当前代码为了避免微信回调超时，先返回 `success`，再通过 FastAPI `BackgroundTasks` 执行 RAG 和客服消息发送。当前 RAG 实测可能超过微信回调的约 5 秒窗口，因此本迁移不改成同步等待。

云托管试运行必须将**最小副本数设为 1，最大副本数先设为 1**，避免实例缩容到 0 后在 ACK 后回收后台任务。这个设置只能降低试运行风险，不能把内存去重和 `BackgroundTasks` 变成持久队列；实例崩溃、发布或网络故障仍可能丢失回复。长期生产方案需要持久化去重状态和可靠 worker/队列，或改成同步被动回复并接受 5 秒超时约束。

### 云托管账号、域名和切换顺序

官方环境文档说明，云托管环境按控制台选择的小程序/公众号主体创建，每个账号最多 3 个环境。本项目无法从代码或未登录控制台确认公众平台「测试号」是否被当前账号允许创建云托管环境；只有登录云托管控制台后能选择该测试号并完成环境创建，才算实际确认。若测试号不在可选列表，需改用已注册的正式公众号或资源复用，不能靠 Docker 代码绕过账号限制。

本轮不绑定 `llink.cyou`，直接使用云托管控制台显示的默认公网域名。官方明确该域名仅适合接口测试，因此只作为当前试运行方案，不把它当作正式生产域名；不需要 CNAME、ICP 备案或自定义证书。

迁移顺序：

1. 在云托管控制台确认测试号可选并创建环境、服务，端口填 `80`。
2. 发布镜像，设置环境变量，最小/最大副本均为 `1`，开启服务公网访问，并设置 `DASHBOARD_ENABLED=false`。
3. 开启开放接口服务，加入 `/cgi-bin/message/custom/send` 云调用白名单；在公众号后台将服务器 URL 填为 `https://<云托管默认域名>/wechat`，Token 填云端 `WECHAT_TOKEN`，不要启用云托管「消息推送」。
4. 查看云日志确认没有 `RAG startup failed`，再用授权微信发送真实问题并确认手机实际收到回复；客服 API 若返回 `48001`，按前文处理账号权限，不把它当作域名问题。
5. 验证完成前保留旧 systemd 服务和旧 Nginx 配置作为回滚材料；本轮不执行自定义域名切换。

云托管官方约束和入口文档： [开发常识](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/debug/know)、[消息推送](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/weixin/push)、[消息推送回复](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/development/weixin/callback)、[环境创建](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/env/)、[部署发布](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/service/online)。

## 查看本地消息看板

浏览器打开 `http://127.0.0.1:8000/`，查看最近文字、RAG 回答、引用和发送状态。回调确认后后台检索并生成；回答含来源。看板每 2 秒刷新，API 返回 `errcode=0` 才显示“客服接口已接受”，不代表手机一定送达。最多保留 100 条于内存，服务重启清空；正文不会写入日志或磁盘，但保存在本地未认证看板中。

看板和 `/messages` JSON 接口没有登录认证，属于**不安全的本地演示**；仅供受信任的本机用户使用。服务绑定 `127.0.0.1`，不要通过 Nginx 或其他方式将 `/`、`/messages` 或整个应用公开到互联网。只反向代理 `/wechat`。

从自己的工作站远程查看时，使用 SSH 本地端口转发（保持 SSH 连接开启）：

```bash
ssh -L 8000:127.0.0.1:8000 <server-user>@<server-host>
```

然后在工作站浏览器打开 `http://127.0.0.1:8000/`。命令左侧第一个 `8000` 是工作站本地端口；若本地 8000 已占用，可改为 8001，例如 `ssh -L 8001:127.0.0.1:8000 <server-user>@<server-host>`，并打开 `http://127.0.0.1:8001/`。

## 公众号回调配置（云托管默认域名）

云端发布并获得默认公网域名后，在公众号后台服务器配置中填写：

| 项目 | 填写内容 |
|---|---|
| URL | `https://<云托管默认域名>/wechat` |
| Token | 与云托管环境变量 `WECHAT_TOKEN` 完全相同 |
| 消息加解密方式 | 明文模式（当前代码不支持安全/兼容模式） |

该路由使用公众号标准签名 XML 回调，不要同时启用云托管「消息推送」，因为消息推送会覆盖该服务器 URL。测试号资格需先在云托管控制台验证。

旧服务器 `.env` 中的 `WECHAT_TOKEN`、`WECHAT_APP_ID`、`WECHAT_APP_SECRET` 仅供现有 systemd 服务及回滚使用。云托管开放接口服务模式使用 `WECHAT_OPEN_API_ENABLED=true`，不依靠这些 App 凭据获取 access_token；客服 API 权限和实际手机送达尚未验证。不要公开 `.env` 或将凭据提交到版本库。

旧 Nginx/systemd 设置仅作为回滚路径保留，不是本轮默认域名部署步骤。

### 旧 Nginx 配置（仅回滚）

以下配置仅用于恢复旧 systemd 服务，不属于云托管默认域名部署步骤。

在旧服务器已有、证书有效的 HTTPS `server` 中添加下面的 location：

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

这是 location 片段，不是完整的 HTTPS 配置。旧服务器公网入口使用 HTTP 80 或 HTTPS 443；本机 8000 不需要向公网开放。如果使用 Cloudflare 代理，回调地址不能出现登录、验证码或 JavaScript 挑战页，也不能缓存回调响应。

比如公网入口为 `https://wx.example.com`，旧服务器回滚时 URL 才填写 `https://wx.example.com/wechat`。

## 验证顺序

### 云托管默认域名

1. 在云托管控制台查看新版本日志，确认服务启动、RAG 初始化没有失败，并检查 `https://<云托管默认域名>/healthz` 返回 `ok`。
2. 在公众号后台填写 `https://<云托管默认域名>/wechat` 和与云环境变量完全一致的 `WECHAT_TOKEN`，使用明文模式提交配置。只有微信后台显示成功，才证明实际签名 GET 验证通过。
3. 从授权微信发送文字，查看云日志中的消息类型、RAG 处理和客服 API 结果；确认手机实际收到回复。`errcode=0` 只代表接口接受，不代表手机送达。

### 旧服务器回滚

1. 旧 systemd 服务本机检查：

   ```bash
   curl --fail http://127.0.0.1:8000/healthz
   ```

   预期响应为 `ok`。本机看板只通过 `http://127.0.0.1:8000/` 或 SSH 隧道访问。
2. 恢复旧 Nginx 反代后，旧服务器公网只开放 `/wechat`；直接访问没有签名的 `/wechat` 返回 HTTP 403 是预期行为。不要公开旧应用的 `/`、`/messages` 或 `/healthz`。
3. 若回滚公众号 URL，填写旧域名的 `/wechat` 和旧服务器 `.env` 中对应的 Token；云托管默认域名配置不变。

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
| `GET /messages` | 看板 JSON：时间、消息正文、RAG 回答与发送状态；不含读者标识 |
| 合法签名的明文文字 `POST /wechat` | HTTP 200，纯文本 `success`；后台使用远程 RAG 查询并尝试发送带来源的回答 |
| 合法签名的明文非文字消息或事件 `POST /wechat` | HTTP 200，纯文本 `success`，不发客服消息 |
| 缺失或错误签名 | HTTP 403 |
| 畸形 XML、DTD、自定义实体、错误根节点或加密消息 | HTTP 400 |
| 超过 256 KiB 的消息体 | HTTP 413，包括分块传输 |

仅支持明文模式，不支持 AES 安全模式或兼容模式。相同 `MsgId` 的文字回调重试仍返回 `success`；若该 ID 仍在最近 100 条记录中，只保留一个看板条目并只安排一次出站发送，避免重复回复。记录淘汰后不再去重。若首次出站失败，重复回调不触发重试；没有 `MsgId` 时，每次回调单独处理。后台任务和记录都只在内存中；进程若在确认回调后、出站请求前退出，回复可能丢失，服务重启后看板清空。没有实现发送重试或持久队列。

客服消息使用 `.env` 的 AppID/AppSecret 获取并缓存 `access_token`；RAG 问题与检索资料片段使用 `rag-poc/.env` 的 embedding/LLM 凭据发送到远程 provider。OpenID 仅发送给微信 API，不发送给模型。微信 access-token 与客服消息 API 请求超时为 5 秒；每次 RAG provider HTTP 请求设为 30 秒且不重试，单条答案可能包含多个请求，整体耗时可能超过 30 秒。对外回复保守限制为 1800 UTF-8 字节，尽量保留来源编号与引用区块。不记录凭据、OpenID 或正文。依官方规则，用户发送消息场景最多发送 5 条客服消息，有效期 48 小时；每条新文字回调最多尝试一条。`errcode=0` 只表示接口接受请求，不保证手机送达。

应用关闭 HTTP 访问日志，只记录已接受消息类型及出站 API 结果码，不记录完整 URL、签名、OpenID、MsgId 或正文。看板最多保留 100 条文字记录于进程内存，服务重启后清空，不保存用户标识。

## 管理与更新

```bash
systemctl status wechat-dev-test.service
sudo ./restart-wechat-dev-test.sh
systemctl stop wechat-dev-test.service
```

修改 unit 或任一 `.env` 后，在仓库根目录运行 `sudo ./restart-wechat-dev-test.sh`。脚本会执行 `systemctl daemon-reload`，确认已加载 unit 引用了根 `.env` 与 `rag-poc/.env`，重启服务，并要求 MainPID 确实变化；对配置文件和 `/proc/<MainPID>/environ` 中变量计算 SHA-256 指纹并仅报告是否匹配，不输出值或哈希。脚本也会拒绝本次启动日志中的 RAG 初始化失败，并等待 `/healthz` 返回 `ok`。需要 root 权限；重启会短暂中断回调，并清空进程内 `access_token` 缓存。

停止且取消开机启动：

```bash
systemctl disable --now wechat-dev-test.service
```

生产依赖与运行文件位于 `rag-poc/`。更新锁定环境：

```bash
cd rag-poc
uv sync --locked --python /usr/bin/python3
```

service 文件将该虚拟环境只读挂载为 `/srv/wechat-dev-test/.venv`，并从根 `.env` 与 `rag-poc/.env` 分别加载微信和 provider 配置。systemd 使用动态非特权 UID；代码、语料和索引只读挂载，NLTK 缓存位于受 `StateDirectory=wechat-dev-test` 保护的数据目录。由于 `DynamicUser` 的公开状态目录是指向 `/var/lib/private/` 的符号链接，`HOME` 和 NLTK 缓存配置使用其实际目录 `/var/lib/private/wechat-dev-test`。修改 unit 或环境文件后使用上述脚本；移动项目目录前必须同步更新 unit 的源路径。用户报告已轮换公众号 AppSecret；`WECHAT_TOKEN` 是否轮换尚未知。最近一次直接 access-token API 探针成功，说明该次请求的 AppID/AppSecret 与 API 来源可获取 access_token；不代表客服消息权限或手机送达已验证。

## 验证

回调测试：`.venv/bin/python -m unittest discover -s tests -v`。RAG 测试：在 `rag-poc/` 中执行 `uv run --locked python -m unittest discover -s tests -v`。本地 smoke 使用合成语料及 loopback provider，不会向微信提交合成 OpenID。

部署后检查 `systemctl is-active wechat-dev-test.service`、`curl --fail http://127.0.0.1:8000/healthz` 与本地看板。仅通过受信任的本机访问或 SSH 隧道查看未认证看板；不要公开代理 `/` 或 `/messages`。API 返回 `errcode=0` 只表示已接受请求，不保证手机送达。真实手机验证应由获授权读者触发。

官方参考：[消息与事件推送](https://developers.weixin.qq.com/doc/subscription/guide/dev/push/)、[发送客服消息](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_sendcustommessage)、[获取 access_token](https://developers.weixin.qq.com/doc/subscription/api/base/api_getaccesstoken)。
