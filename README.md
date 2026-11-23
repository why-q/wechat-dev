# 微信公众号回调与馆藏 RAG

用户最新反馈：公众号已切换到微信云托管「消息推送」，本 README 以下以云内推送为当前接入方式。云托管控制台配置、真实请求头值和 RAG 到手机的完整投递链路未由本次助手独立核验。普通公众号服务器 URL、旧 Nginx 和 systemd 服务只作为回滚路径。

线上 RAG 会把读者问题正文及命中的馆藏片段发送到运行环境配置的远程 embedding/chat provider。云托管使用服务环境变量，旧 systemd 服务使用 `rag-poc/.env`；OpenID 仅传给微信客服消息 API。用户已确认数据处理授权及 provider 数据区域/留存条款。provider 合成连通性探针通过。一次本机签名合成文字回调获得 `success` 并生成带来源编号的回答，但返回摘录未清楚支持答案，引用相关性尚未证明。此前合成 OpenID 的客服消息尝试返回 `errcode=48001`；之后一次 access-token API 探针成功，但实际手机送达尚未确认。答案与拒答能力尚未系统评测，检索阈值 `0.2` 未校准。

## 微信云托管消息推送

代码侧已经增加微信云托管入口，相关文件为 `Dockerfile` 和 `.dockerignore`。镜像内固定使用 `0.0.0.0:${PORT}`，RAG 包通过 `PYTHONPATH=/app/rag-poc` 加载，索引和 4 份 Markdown 语料作为镜像只读内容发布。

当前入口是云托管「消息推送」的云内链路，不是云托管默认公网域名。默认域名不填写为公众号回调 URL；它只供运维人员主动访问服务接口，例如检查 `/healthz`。公网访问关闭后无法通过该域名检查。不要为此重新开启公网访问，否则 `/wechat` 也会暴露在公网，增加无签名请求风险。

### 云托管协议差异

云托管推送不带公众号 `signature`，而是通过带 `x-wx-source` 的明文 JSON 或 XML 推送到 `/wechat`。代码只有在 `WECHAT_CLOUD_PUSH_ENABLED=true` 且请求带有非空 `x-wx-source` 时才进入该分支；否则会尝试标准签名校验。当前实现只检查请求头是否存在，不校验其值或来源，因此公网访问必须关闭。

云托管检测请求也走 `/wechat`：

```json
{"action":"CheckContainerPath"}
```

`CheckContainerPath` 请求由应用返回 HTTP 200 和 `success`。控制台路径填 `/wechat`，格式建议选 JSON。配置确认如要求扫码，应由目标公众号管理员扫码；资源复用场景下，微信官方明确要求目标小程序或公众号管理员，而非环境管理员。启用云托管推送会覆盖原公众号服务器 URL；解绑后，原 URL 不会自动恢复，需先在公众号后台停用服务器配置，再重新启用。

客服消息在云端使用微信云托管「开放接口服务」：设置 `WECHAT_OPEN_API_ENABLED=true` 后，应用向 `http://api.weixin.qq.com/cgi-bin/message/custom/send` 发送 JSON，不再获取或拼接 `access_token`。云托管控制台必须开启开放接口服务，并将 `/cgi-bin/message/custom/send` 加入云调用白名单。不要在云端关闭该开关再直连微信 API：云托管没有固定公网出口 IP，直连方案无法依赖公网 IP 白名单。

开放接口服务只改变云调用鉴权和路由，不自动授予公众号客服消息权限。此项目此前对同一账号的直连 `custom/send` 曾收到 `errcode=48001`，因此云端客服发送目前只能视为**未完成真实验证**；若云调用仍返回 `48001`，需要在账号侧开通相应客服消息能力，或改用公众号被动回复。被动回复必须在同一个消息推送响应中完成并受约 5 秒限制，当前远程 RAG 耗时不适合直接改成被动 RAG 回复。

云托管服务应关闭公网访问，消息通过云内链路投递。`x-wx-source` 不是签名或密码；当前代码只判断该头是否非空，若公网访问开启，任何外部请求都可能伪造该头并跳过签名校验。应用不会记录该头的实际值，不能从现有日志确认它。需要核对时，先确保公网访问关闭，再临时加入受控诊断记录，触发一次真实推送并观察该值；确认后移除诊断记录，将代码改为精确校验实际值，或同时限制可信内网来源，并测试拒绝任意请求头。精确匹配本身不一定构成公网认证，来源仍可被公网伪造时，必须继续关闭公网访问。

### 云端环境变量

云托管控制台的服务环境变量至少设置：

```text
WECHAT_CLOUD_PUSH_ENABLED=true
WECHAT_OPEN_API_ENABLED=true
DASHBOARD_ENABLED=false

RAG_LLM_API_BASE=...
RAG_LLM_API_KEY=...
RAG_LLM_MODEL=...
RAG_EMBEDDING_API_BASE=...
RAG_EMBEDDING_API_KEY=...
RAG_EMBEDDING_MODEL=...
```

云推送模式不需要 `WECHAT_TOKEN`、`WECHAT_APP_ID` 或 `WECHAT_APP_SECRET` 来接收入站消息或调用客服消息 API。旧 systemd 回滚配置仍使用旧服务器自己的环境变量。云端客服消息通过开放接口服务发送，控制台还需开启该服务并把 `/cgi-bin/message/custom/send` 加入云调用白名单。所有微信凭据和 provider key 只放在云托管环境变量，不要放入镜像、Dockerfile 或代码包。

### 临时 HTTP 查询 RAG

需要通过云端调试面板验证 RAG 时，可临时设置独立的高熵 `RAG_DEBUG_TOKEN` 并重启服务。不要复用 `WECHAT_TOKEN` 或 provider key；未设置该变量时接口返回 404。

在云托管「云端调试」中发送：

```http
POST /debug/rag/query
Authorization: Bearer <RAG_DEBUG_TOKEN>
Content-Type: application/json

{"question":"合成问题：开放时间是什么？"}
```

成功时返回 `{"answer":"..."}`，包含 RAG 回答和来源；该接口直接查询 RAG，不调用客服消息 API，也不把问题或答案写入应用的本地消息看板。问题上限 2000 字符；查询会访问远程 provider 并可能产生费用。调试完成后移除 `RAG_DEBUG_TOKEN` 并重启服务，使接口回到 404。若面板不支持自定义 `Authorization` 请求头，可在 WebShell 中对 `http://127.0.0.1:<端口>/debug/rag/query` 发送相同请求。

查询失败时返回 HTTP 502 和阶段码：`embedding_retrieval_failed`（embedding/向量检索）、`retrieval_filter_failed`（检索结果筛选）、`llm_synthesis_failed`（LLM 生成）、`answer_formatting_failed`（回答格式化）、`empty_llm_response`（模型返回空回答）、`citation_validation_failed`（引用校验）或 `query_setup_failed`（查询准备）。公众号文字回调的云日志也记录相同阶段码和异常类型；启动日志可见 `rag_configuration_invalid`、`rag_local_setup_failed`、`index_missing`、`index_load_failed` 或 `rag_initialization_failed`，未就绪时接口返回 503。响应只包含安全提示；日志不记录 provider 异常文本、URL 或凭据。客服 API 的 `errcode`（例如 `48001`）是独立的微信发送结果，需按账号能力和 API 授权另行排查。

#### 公众号无回复时查看云端日志

升级到含有链路日志的镜像后，按同一个 `record_id` 查看：`WeChat reply task scheduled` → `WeChat reply task started` → `RAG reply generation started` → `RAG reply generation completed` 或 `RAG response unavailable` → `WeChat reply send started` → 微信 API 结果 → `WeChat reply task finished`。耗时以毫秒记录。日志不会包含问题正文、OpenID、MsgId、回复内容、URL、Token 或 provider 原始异常。

- 只有 `WeChat callback accepted`，没有 `reply task scheduled`：检查后续是否有 `duplicate_message` 或 `rag_not_ready`；若都没有，确认部署的是最新镜像。
- 有 `scheduled`，没有 `task started`：后台任务未开始或进程在执行前退出；FastAPI `BackgroundTasks` 是进程内任务，实例重启/回收时不保证投递。
- 有 `RAG reply generation started`，没有 `completed` 或 `RAG response unavailable`：查询仍在运行或进程退出；如果持续如此，记录该条 `record_id` 和日志时间段。
- 有 `RAG response unavailable`：按 `code` 排查 RAG 阶段。
- 有 `reply send started` 后出现 `errcode` 或 `api_request_error`：检查对应微信 API 错误码、`mode`（云托管开放接口服务或 access-token 直连）及云调用白名单/账号权限。
- `WeChat customer message API accepted: errcode=0` 只表示接口接受发送请求，不保证用户手机端已收到。

`reply task skipped: reason=duplicate_message` 表示相同微信消息 ID 已处理过；日志有意不输出该 ID。

这些阶段码和回调链路日志需要部署包含该代码的新版镜像后才会生效；只改环境变量或重启旧版本不会更新诊断信息。

### 本地构建与回归检查

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

本地 Docker smoke 检查 `/healthz`；单元测试覆盖 `CheckContainerPath`、`x-wx-source` 云推送分支和普通签名回调。它们不能代替云托管控制台的路径检测或真实微信消息验证。

手动上传代码包的官方大小上限为 2 MiB；如果代码包超过上限，应改用 Git 仓库流水线或镜像仓库。`rag-poc/index/` 已取消 Git 忽略并必须随 GitHub 构建上下文提交；语料或索引更新需重新提交并发布。

本次镜像 smoke 还使用占位 provider endpoint 实例化了 `RagResponder()`，输出 `RAG_RESPONDER_INIT_COMPLETE`；这证明镜像索引可加载，不代表真实 provider 网络或客服 API 权限已验证。

### 使用 GitHub 绑定构建

推送前先检查 `git status` 和 staged diff，只加入需要部署的源码、`Dockerfile`、`.dockerignore`、`rag-poc/pyproject.toml`、`rag-poc/uv.lock`、语料及 `rag-poc/index/`；不要加入任何 `.env`、API key、微信凭据、`.venv/`、`.cache/` 或本机配置。索引约 1.24 MB，已从两层 `.gitignore` 中取消忽略，以便 GitHub 构建上下文包含它；索引/语料更新需随代码重新提交。

当前仓库在 GitHub 上为 **Public**；本轮已确认当前提交内容可公开。历史审计未发现 `.env` 或 `rag-poc/.env` 曾被提交；README、systemd 配置、服务器路径、域名和馆藏语料仍属于部署信息，后续公开变更应继续复核。当前本地 `master` 跟踪 GitHub `main`，后续部署提交应推送到 `main`。

在微信云托管控制台选择「绑定 GitHub 仓库」时，授权 GitHub 应用仅访问此仓库，选定部署分支，并把 Dockerfile 路径设为仓库根目录 `Dockerfile`、构建上下文设为仓库根目录、服务端口设为 `80`。确认构建日志实际包含 `rag-poc/index` 且镜像构建成功后，再发布服务版本。不要把 `.env` 上传到 GitHub；provider keys、`WECHAT_TOKEN` 等只通过云托管环境变量设置。

GitHub 绑定只负责取代码和构建，不会替代之前列出的环境变量、最小副本、开放接口服务、云调用白名单或公众号后台 URL 配置。

### 副本数与异步任务硬约束

当前代码为了避免微信回调超时，先返回 `success`，再通过 FastAPI `BackgroundTasks` 执行 RAG 和客服消息发送。当前 RAG 实测可能超过微信回调的约 5 秒窗口，因此本迁移不改成同步等待。

云托管试运行必须将**最小副本数设为 1，最大副本数先设为 1**，避免实例缩容到 0 后在 ACK 后回收后台任务。这个设置只能降低试运行风险，不能把内存去重和 `BackgroundTasks` 变成持久队列；实例崩溃、发布或网络故障仍可能丢失回复。长期生产方案需要持久化去重状态和可靠 worker/队列，或改成同步被动回复并接受 5 秒超时约束。

### 云托管账号与环境配置

用户已报告在云托管完成公众号消息推送配置，当前试运行不再以测试号能否创建环境作为前置条件。云托管环境对应的公众号主体、环境归属及是否使用资源复用，尚未由本助手独立核验；新建或迁移到其他环境时，仍需以控制台可选主体和实际绑定关系为准。官方环境文档说明每个账号最多创建 3 个环境。

默认域名不是消息推送入口，不绑定 `llink.cyou`，也不配置 CNAME、ICP 备案或自定义证书。关闭公网访问后，不应为了访问默认域名的 `/healthz` 而重新公开服务。

### 当前配置与运行验收

用户报告已在云托管配置消息推送。以下为这条接入方式的运行条件和验收顺序，云端实际值仍以控制台为准：

1. 服务端口设为 `80`，`WECHAT_CLOUD_PUSH_ENABLED=true`、`WECHAT_OPEN_API_ENABLED=true`、`DASHBOARD_ENABLED=false`。最小和最大副本在试运行期间都设为 `1`。
2. 关闭服务公网访问。云托管「消息推送」选择当前环境和服务，路径填 `/wechat`，格式建议选 JSON。
3. 将 `/cgi-bin/message/custom/send` 加入云调用白名单，并开启开放接口服务。
4. 控制台 `CheckContainerPath` 检测必须返回 HTTP 200 和 `success`。若返回非 200（例如 403 或 503），检测未通过；403 可能表示请求未带 `x-wx-source`，导致应用进入 `_verify_signature`，503 则需检查服务是否就绪。先查明原因，不能把检测失败当作已接通。本次对话未核验控制台实际返回码。
5. 用授权微信发送真实文字，确认云端收到推送、RAG 生成和客服 API 结果正常，并确认手机收到回复。API 返回 `errcode=0` 只代表接口接受请求，不保证手机送达。
6. 配置确认如要求扫码，由目标公众号管理员完成；资源复用时必须由目标公众号管理员扫码。旧 systemd/Nginx 保留为回滚路径，不作为当前消息入口。

用户已报告切换到云托管消息推送，但真实推送中的 `x-wx-source` 值尚未核实。需要核对时，先确保公网访问关闭，再临时加入受控诊断记录并触发一次真实推送。确认后移除诊断记录，将代码改为精确校验实测值，或同时限制可信内网来源，并测试拒绝任意请求头。精确匹配本身不一定构成公网认证；若来源仍可被公网伪造，必须继续关闭公网访问。

云托管官方约束和入口文档：[开发常识](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/debug/know)、[消息推送](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/weixin/push)、[消息推送回复](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/development/weixin/callback)、[环境创建](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/env/)、[部署发布](https://developers.weixin.qq.com/miniprogram/dev/wxcloudservice/wxcloudrun/src/guide/service/online)。

## 查看旧 systemd 服务的本地消息看板

旧 systemd 服务的看板位于 `http://127.0.0.1:8000/`，显示最近文字、RAG 回答、引用和发送状态。后台处理会在回调确认后运行；API 返回 `errcode=0` 才显示“客服接口已接受”，不代表手机一定送达。看板每 2 秒刷新，最多保留 100 条于内存，服务重启后清空。正文不写入日志或磁盘，但会保存在本地未认证看板中。

看板和 `/messages` JSON 接口没有登录认证，属于**不安全的本地演示**，仅供受信任的本机用户使用。旧服务绑定 `127.0.0.1`，不要通过 Nginx 或其他方式将 `/`、`/messages` 或整个应用公开到互联网。只反向代理 `/wechat`。

从自己的工作站远程查看时，使用 SSH 本地端口转发（保持 SSH 连接开启）：

```bash
ssh -L 8000:127.0.0.1:8000 <server-user>@<server-host>
```

然后在工作站浏览器打开 `http://127.0.0.1:8000/`。命令左侧第一个 `8000` 是工作站本地端口；若本地 8000 已占用，可改为 8001，例如 `ssh -L 8001:127.0.0.1:8000 <server-user>@<server-host>`，并打开 `http://127.0.0.1:8001/`。

## 云托管消息推送配置

用户报告已将公众号消息推送切换到云托管。公众号消息由云托管通过内网链路投递到所选服务，不使用默认公网域名作为回调 URL。云托管推送开启后会覆盖原公众号服务器 URL；关闭服务公网访问，不要为了访问默认域名的 `/healthz` 而暴露 `/wechat`。

当前代码要求 `WECHAT_CLOUD_PUSH_ENABLED=true` 且 POST 请求带有非空 `x-wx-source` 才进入云推送分支。缺少该头时会尝试普通签名校验，因此控制台的 `CheckContainerPath` 检测必须得到 HTTP 200 和 `success`。应用日志不记录该头的实际值；现阶段不能仅凭应用日志确认其值，也不要在公网访问开启时依赖「请求头存在」作为来源认证。

客服消息通过云托管开放接口服务发送。确认服务已开启，并将 `/cgi-bin/message/custom/send` 加入云调用白名单。`DASHBOARD_ENABLED=false` 时看板和 `/messages` 返回 404。旧服务器 `.env`、Nginx 和 systemd 只用于回滚，不是当前消息入口。解绑云托管推送后，需先到公众号后台停用服务器配置，再重新启用，原服务器 URL 才会恢复。

不要公开 `.env` 或将凭据提交到版本库。

### 旧 Nginx 配置（仅回滚）

以下配置仅用于恢复旧 systemd 服务，不属于当前云托管消息推送路径。

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

这是 location 片段，不是完整的 HTTPS 配置，仅用于旧服务器回滚。本机 8000 不需要向公网开放。如果使用 Cloudflare 代理，旧回调地址不能出现登录、验证码或 JavaScript 挑战页，也不能缓存回调响应。

## 微信历史问题采集（一次性 CLI）

`dataset-sync` 直接通过官方 HTTPS `getmsglist` 查询客服历史，不能走云调用；账号必须是已认证的公众号/服务号并具备相应客服历史权限。请在出口 IP 已加入公众号 API 白名单的服务器运行，使用受保护的 shell 环境或权限为 `0600` 的外部 env 文件设置 `WECHAT_APP_ID`、`WECHAT_APP_SECRET` 和稳定生成的 `WECHAT_DATASET_HASH_KEY`。这些值不得写入仓库、命令历史、CSV 或日志。

服务 mount namespace 中才会提供 `/srv/wechat-dev-test/rag_poc`；宿主机上的同名路径可能只是空目录。因此下面示例假定 standalone 包和虚拟环境确实已安装在 `/srv/wechat-dev-test`，并从该项目根目录运行。若使用仓库 checkout，请从 `rag-poc` 目录运行 `./.venv/bin/python -m rag_poc.cli dataset-sync`，并保留相同参数。这个初始窗口包含中国时区的“前 30 天至今天”（首尾包含）：

```bash
cd /srv/wechat-dev-test
set -a; . /etc/wechat-dataset.env; set +a
DATASET_START_DATE="$(TZ=Asia/Shanghai date -d '30 days ago' '+%F')"
DATASET_END_DATE="$(TZ=Asia/Shanghai date '+%F')"
/srv/wechat-dev-test/.venv/bin/python -m rag_poc.cli dataset-sync \
  --start-date "$DATASET_START_DATE" \
  --end-date "$DATASET_END_DATE" \
  --output-dir /srv/wechat-dataset
```

输出目录在仓库外，包含 `questions.csv`、`sync-state.json` 和锁文件。重复运行同一窗口会按游标继续；失败或中断后应复用原先保存的两个日期参数，即使已经跨过午夜也不要重新计算日期。换日期范围时才使用 `--reset`，它保留 CSV 并依靠稳定 HMAC key 去重。游标按中国本地日和 `msgid` 推进：完整历史日进入下一日，当前日短页停在当前日的 `next_msgid=1`，稍后重跑可捕获新消息。

清理时先把 canonical CSV 复制到 collector 管理目录之外的受保护位置，绝不直接编辑 canonical 文件。用表格软件导入副本时，明确将 `question` 列指定为**文本**（不要直接双击 CSV），避免读者问题开头的 `=` 被当作公式。清理副本中移除 `record_key`，也不含 OpenID。

采集期间主 API 使用 `WECHAT_AUTO_REPLY_ENABLED=false` 且保持公网访问关闭：合法云推送或签名回调仍返回 `200 success`，但不会初始化 RAG、回答、排队任务或外呼。授权的实时跟进消息需在该模式下再次运行当前中国日采集验证；完成后仅在需要恢复原有 RAG 路径时将开关改回 `true`。

真实验证前须确认目标服务器已获授权、出口 IP 已加入微信 API 白名单、账号客服历史能力已开通，并已在受保护环境中设置稳定 hash key。这些是运行前置条件，需由运维核实。

## 验证顺序

### 云托管消息推送

1. 在云托管控制台检查服务日志，确认启动与 RAG 初始化正常。公网访问关闭时，不要通过默认域名访问 `/healthz`，改用控制台状态与日志。
2. 在「设置 → 全局设置 → 消息推送」中确认环境、服务和 `/wechat` 路径。控制台 `CheckContainerPath` 必须返回 HTTP 200 和 `success`，否则先排查环境变量及 `x-wx-source` 是否进入当前代码的云推送分支。
3. 从授权公众号发送真实文字，确认云端收到消息、客服 API 返回结果，并核对手机实际收到回复。`errcode=0` 只表示微信 API 接受请求。
4. 确认云托管服务仍关闭公网访问，直到代码的请求来源校验不再只依赖请求头存在；如果为诊断而记录 `x-wx-source`，只在该诊断版本和真实推送期间临时启用，确认后立即移除记录并改为精确匹配或可信内网来源判断。

### 旧服务器回滚

1. 旧 systemd 服务本机检查：

   ```bash
   curl --fail http://127.0.0.1:8000/healthz
   ```

   预期响应为 `ok`。本机看板只通过 `http://127.0.0.1:8000/` 或 SSH 隧道访问。
2. 恢复旧 Nginx 反代后，旧服务器公网只开放 `/wechat`；直接访问没有签名的 `/wechat` 返回 HTTP 403 是预期行为。不要公开旧应用的 `/`、`/messages` 或 `/healthz`。
3. 先在云托管解绑消息推送，再到公众号后台停用并重新启用服务器配置，之后填写旧域名的 `/wechat` 和旧服务器 `.env` 中对应的 Token。解绑不会自动恢复原服务器 URL。

### 微信后台消息入口的边界

官方普通公众号服务器回调文档说明，用户消息会转发到配置的开发者 URL，启用后网页中设置的自动回复和自定义菜单失效。该说明针对普通服务器 URL 接入；本项目当前使用云托管消息推送，消息通过云内链路送达，不应把默认公网域名当作当前入口。

用户确认，工作人员使用微信公众平台自带网页后台处理私信，但未确认具体入口是普通“消息管理”还是公众号“客服功能”。两者是不同入口，不能把其中任何一个当作已确认的实际页面。公众号[客服功能](https://developers.weixin.qq.com/doc/subscription/guide/product/kf/intro.html)是独立的网页客服工作台；当前 demo 没有返回 `MsgType=transfer_customer_service`，不会转入该工作台。独立产品“微信客服”是另一套产品，本项目尚未接入。

官方[获取聊天记录接口](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_getmsglist)用于查询客服聊天记录，单次查询时间跨度不超过 24 小时，每次最多 10,000 条。公众号和服务号的接口适用范围均为“仅认证”。这些是 `getmsglist` 查询 API 的限制，不代表整个客服功能的开通要求，也不代表聊天记录保存时长；官方开发者文档未给出记录保存期限。

官方[发送客服消息 API 说明](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_sendcustommessage)规定，用户发送消息场景的客服消息额度最多 5 条，有效期为 48 小时；这是发送额度及有效期，不是聊天记录保存期限。

云托管试运行时，从授权微信发送文字，查看云托管日志中的消息类型和客服 API 结果，并确认手机收到回复。云端应设置 `DASHBOARD_ENABLED=false`，所以旧 systemd 部署的本地看板不能用于判断云端消息结果。

## 接口与边界

| 接口或请求 | 结果 |
|---|---|
| `GET /healthz` | HTTP 200，纯文本 `ok` |
| `GET /` | 本地消息看板 HTML；未做身份认证 |
| `GET /messages` | 看板 JSON：时间、消息正文、RAG 回答与发送状态；不含读者标识 |
| 合法签名的明文文字 `POST /wechat` | 普通公众号服务器回调兼容路径：HTTP 200，纯文本 `success`；后台使用远程 RAG 并尝试发送回答 |
| 云托管推送 `POST /wechat` | 仅当 `WECHAT_CLOUD_PUSH_ENABLED=true` 且带非空 `x-wx-source` 时按 JSON/XML 解析；文字消息快速返回 `success`，随后在后台处理 RAG 与客服发送 |
| 云托管 `CheckContainerPath` | 云推送分支返回 HTTP 200 和 `success`；未进入该分支时可能因签名无效返回 HTTP 403，服务不可用等也可能返回其他非 200（例如 503）。控制台实际返回码未核验 |
| 畸形 XML、DTD、自定义实体、错误根节点或加密消息 | HTTP 400 |
| 超过 256 KiB 的消息体 | HTTP 413，包括分块传输 |

仅支持明文模式，不支持 AES 安全模式或兼容模式。相同 `MsgId` 的文字回调重试仍返回 `success`；若该 ID 仍在最近 100 条记录中，只保留一个看板条目并只安排一次出站发送，避免重复回复。记录淘汰后不再去重。若首次出站失败，重复回调不触发重试；没有 `MsgId` 时，每次回调单独处理。后台任务和记录都只在内存中；进程若在确认回调后、出站请求前退出，回复可能丢失，服务重启后看板清空。没有实现发送重试或持久队列。

云托管客服消息使用 `WECHAT_OPEN_API_ENABLED=true` 时通过开放接口服务发送；旧 systemd 回滚模式使用旧 `.env` 中的 AppID/AppSecret 获取并缓存 `access_token`。RAG 问题与检索资料片段使用云端服务环境变量或旧 systemd `rag-poc/.env` 中的 embedding/LLM 凭据发送到远程 provider，OpenID 仅发送给微信 API，不发送给模型。微信 API 请求超时为 5 秒；每次 RAG provider HTTP 请求设为 30 秒且不重试，单条答案可能包含多个请求，整体耗时可能超过 30 秒。对外回复保守限制为 1800 UTF-8 字节，尽量保留来源编号与引用区块。不记录凭据、OpenID 或正文。依官方规则，用户发送消息场景最多发送 5 条客服消息，有效期 48 小时；每条新文字回调最多尝试一条。`errcode=0` 只表示接口接受请求，不保证手机送达。

应用关闭 HTTP 访问日志，只记录已接受消息类型及出站 API 结果码，不记录完整 URL、签名、`x-wx-source`、OpenID、MsgId 或正文。因此现有应用日志不能用于核对 `x-wx-source` 的实际值。

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

旧 systemd 回滚路径可检查 `systemctl is-active wechat-dev-test.service`、`curl --fail http://127.0.0.1:8000/healthz` 和本地看板。当前云托管消息推送应按「云托管消息推送」验收，不要将旧服务器健康检查当作云端状态证明。仅通过受信任的本机访问或 SSH 隧道查看未认证看板；不要公开代理 `/` 或 `/messages`。真实手机验证应由获授权读者触发。

官方参考：[消息与事件推送](https://developers.weixin.qq.com/doc/subscription/guide/dev/push/)、[发送客服消息](https://developers.weixin.qq.com/doc/subscription/api/customer/message/api_sendcustommessage)、[获取 access_token](https://developers.weixin.qq.com/doc/subscription/api/base/api_getaccesstoken)。
