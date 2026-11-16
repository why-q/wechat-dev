# RAG PoC 进度

## 当前状态

公众号回调代码已接入 LlamaIndex `RagResponder`：文字消息在返回 `success` 后经远程 embedding/chat API 查询本地索引，再通过微信客服 API 发送带来源的回答；OpenID 不传给模型。若 RAG 初始化失败，服务仍启动以提供 health 和非文字回调，但文字回调返回 HTTP 503 且不确认；查询失败则发送通用回复，不记录 provider 异常。

新版 RAG unit 已经 daemon-reload 并重启；systemd 加载根 `.env` 和 `rag-poc/.env`。用户最近运行重启脚本并报告服务 active、RAG 初始化成功、`/healthz` 返回 ok。最近一次直接调用微信 access-token API 成功，返回有效期 7200 秒；这证明当时 AppID/AppSecret 与 API 调用来源可获取 access_token，不代表客服消息已送达。用户报告已轮换公众号 AppSecret；`WECHAT_TOKEN` 是否轮换或与后台一致尚未确认。用户浏览器访问公网 `/wechat` 得到 `invalid signature`，与请求到达应用但未通过签名校验相符，不代表微信后台验证成功；公网 `/health` 返回 404 是因为 Nginx 只反代 `/wechat`，健康端点 `/healthz` 仅供本机检查。用户已确认所需数据处理授权及 provider 数据区域/留存条款；合成 live provider 探针通过。一次本机签名合成文字回调得到 `success`，RAG 生成了带引用编号的回答，但所列摘录未清楚支持答案，引用相关性仍需评估；此前合成 OpenID 的客服消息尝试返回 `errcode=48001`，未证明手机送达。

## 微信云托管迁移

代码侧已完成第一版云托管适配：新增根目录 `Dockerfile` 与 `.dockerignore`，镜像内通过 `PYTHONPATH=/app/rag-poc` 加载 `rag_poc` 包，并打包当前索引和语料。用户已选定直接使用云托管默认公网域名，不绑定个人域名；因此实际部署路线为公网入口 + 原有公众号 signature/XML 协议，设置 `WECHAT_CLOUD_PUSH_ENABLED=false`，不启用会覆盖公众号服务器 URL 的云托管「消息推送」。云推送 JSON/XML 和 `CheckContainerPath` 仅作为关闭公网访问时的备选分支保留。客服消息在配置开放接口服务后通过云调用发送，但账号权限和线上送达尚未验证；`DASHBOARD_ENABLED=false` 时 `/` 和 `/messages` 返回 404。

本地回归测试 19/19 通过。本地 Docker smoke 已验证镜像内 `import rag_poc`、`/healthz`、云托管路径检测、普通签名 GET echo，以及看板关闭后的 404；使用占位 provider endpoint 实例化 `RagResponder()` 也成功，证明当前索引能被镜像加载，但不代表真实 provider 网络可用。镜像构建使用锁定的 `rag-poc/uv.lock`，当前 Docker 构建工具固定为 uv 0.10.12。

云端尚未发布，原因不是代码 blocker，而是需要在已登录的云托管控制台完成主体和服务配置：官方文档说明环境按所选小程序/公众号主体创建，但没有在文档中明确承诺公众平台测试号资格；必须实际确认测试号能否被选择并创建环境。当前路线需要开启服务公网访问、发布端口 80、设置最小/最大副本为 1、设置 `DASHBOARD_ENABLED=false`、开启开放接口服务并将 `/cgi-bin/message/custom/send` 加入云调用白名单，然后把公众号服务器 URL 指向云托管默认域名的 `/wechat`，不要启用会覆盖该 URL 的云托管「消息推送」。云托管没有固定公网出口 IP，不能配置旧服务器 IP 作为直连微信 API 白名单；默认公网域名仅用于接口测试，本轮接受该限制，不绑定个人域名。此前同一账号直连客服 API 曾返回 `48001`，开放接口服务不会自动补齐账号权限，云端客服发送仍未真实验证；若仍为 `48001`，需开通账号能力或改用受 5 秒限制的被动回复。

当前回调为快速 ACK 后执行 `BackgroundTasks`。因为 RAG 实测可能超过微信约 5 秒回调窗口，本轮不改为同步等待；云托管试运行必须保持最小副本 1、最大副本 1。这仍不是持久队列，实例崩溃、发布或网络故障可能丢失回复；多副本和正式可靠投递前需要持久化状态及 worker/队列。

## PoC 已创建

已新增 `rag-poc/pyproject.toml`、`rag-poc/uv.lock`、独立 `.venv`、`rag-poc/rag_poc/cli.py`、README 和本地测试。依赖使用 LlamaIndex core、file reader、OpenAI-compatible LLM/embedding adapter 与 dotenv；仅安装在 PoC `.venv`。

`rag-poc/corpus/` 已创建，当前有用户添加的 4 份 Markdown 馆藏文档。索引命令支持可抽取文本的 PDF、TXT 和 Markdown，以 `SimpleVectorStore` 建索引，并通过 LlamaIndex storage context 持久化向量、docstore 与 index store 到默认 `rag-poc/index/`。每个 chunk 保留 reader 提供的文件和可用页码/章节 metadata。

问题命令使用 LlamaIndex `CitationQueryEngine`，普通 chat 模式关闭 function calling。检索后先按 `min_score` 筛证据，未达门槛时输出固定拒答文案且不调用生成；达标时调用模型，但模型也可能自行拒答。来源序号会与 `source_nodes` 校验，引用列出语料相对路径和可用页码/章节，并附从对应 citation chunk 本文提取、折叠空白后截断到最多 40 个 Unicode 字符的摘录，不由模型生成。当前 `min_score=0.2` 尚未经过系统评测校准，答案与引用质量也未系统评测。

`.env.example` 和 README 列出 PoC 的六项 provider 配置。生产 service 与回调代码现已改为使用 PoC 的锁定虚拟环境、索引和 provider 配置；未修改根项目依赖或 Nginx。

已定位分词失败原因：锁定环境中的 NLTK `pathsec.py` `_hardened_open()` 在 `st_nlink > 1` 时拒绝打开文件（第 2238 至 2243 行）。LlamaIndex 随包的 stopwords 34 个文件和 punkt_tab 77 个文件均为 `nlink=2`、权限 `0644`、属主 root，因此触发安全拒绝。用临时私有目录准备独立资源后，`wait_for_nltk_check()` 和 Punkt tokenizer 均成功，资源文件 link count 全为 1；没有下载数据或改动 site-packages。

PoC 现在会在 index/query 初始化 SentenceSplitter 前，把 LlamaIndex bundled stopwords 和 punkt_tab 复制到 `rag-poc/.cache/nltk_data/`，后续复用副本。目录权限设为 `0700`，文件权限设为 `0600`，并检查属主、文件类型与 link count；不关闭 NLTK 安全 guard，也不联网下载 NLTK 数据。新增测试会实际运行 SentenceSplitter，并验证缓存重复初始化时不会重复制。

PoC 独立 `.venv` 中 `uv sync --locked` 成功。来源摘录修复后的最终代码运行 22 项 unittest，全部通过（`uv run --locked python -m unittest discover -s tests -v`，2.205 秒）。本地分块 smoke 生成 1 个 chunk。真实索引命令处理语料中的 4 份 Markdown，生成 34 个文本块并保存向量、docstore 和 index store。索引后和加入来源摘录后都运行过真实相关 query；最近一次退出码为 0，耗时 17.591 秒，返回 4 个来源，均含对应 citation chunk 摘录。实际调用使用配置的远程 embedding 与 chat API，未调用微信接口；回答与摘录正文未写入此记录。测试集本身不调用远程 provider API。

此前一次 `check-config --live` 曾观察到 embedding 探针通过、chat 探针未通过；之后实际 index 与 query 均成功。另对服务项目、规章制度借阅期限/逾期、火星基地三个合成问题各运行一次真实查询，均以退出码 0 完成，耗时分别为 16.682 秒、10.631 秒和 8.177 秒，每次返回 4 个 corpus 内来源。只检索不生成的探针对每题取回 4 个节点：服务项目分数最低为 0.628630，最高为 0.654950；规章制度问题最低为 0.673374，最高为 0.716506；火星无关问题最低为 0.312753，最高为 0.329364。当前 `min_score=0.2` 时，这三组观测节点均过门槛。本次小样本的经验分隔区间为 `0.329364 < min_score <= 0.628630`，但它不是经校准的通用阈值，本轮未调整阈值。

本次火星无关问题的检索节点全部超过当前 `min_score=0.2`，因此系统继续调用 LLM 生成回答。对这一次实际回答的脱敏分类是“明确拒答/资料不足”，未虚构火星基地，也没有输出固定 `ABSTENTION_TEXT`。这次观察到的失败模式是多发生了一次 LLM 生成调用，而不是观察到编造答案；这只描述本次样本，不构成通用安全保证。检索门槛拒答和模型自然拒答是两条不同路径。回答正文未写入记录。答案事实、引用和拒答能力仍未系统评测，也未测量成本。

已新增 `check-config` 子命令。默认路径和显式的 `--only-config` 只复用 `load_api_config(require_llm=True)` 做六项变量齐全与 API base URL 格式检查，不发网络请求，也不能证明 key、model 或远程 endpoint 有效；`--live` 才使用现有 OpenAI-like adapters 向 chat 与 embedding endpoint 各发送一次固定合成探针。探针不含真实读者消息、OpenID 或个人信息，设置约 10 秒超时且显式 `max_retries=0`；index 与 query 不传该参数，保留 adapter 默认重试。探针会产生网络流量并可能产生费用。命令只输出 provider 主机名、model 摘要和通过项，失败时不输出 SDK 异常、响应正文、请求头、key 或完整 URL。

用户可从 `rag-poc/` 运行 `uv run --locked python -m rag_poc.cli check-config`，或用 `check-config --only-config` 显式表示离线路径；缺配置或格式错误以非零状态退出。只有在确认允许产生远程流量和可能费用、并已提供真实凭据时才运行 `check-config --live`；两类探针均通过才返回零状态。永久测试仅把 `--live` 指向 127.0.0.1 临时 server，不连接真实 provider，也不会调用微信或发送真实读者数据。

## 生产接入与风险

实时 RAG 会将真实读者问题正文和检索到的馆藏片段发送到 `rag-poc/.env` 配置的远程 embedding/chat provider；OpenID 仅用于微信客服消息 API。用户已确认数据处理授权及 provider 数据区域/留存条款；答案事实、引用、拒答能力和 `min_score=0.2` 仍未系统评测。

服务故障时发通用回复，不暴露 provider 异常；非文字回调不调用 RAG。正式试运行仍待验证：最近一次直接 access-token API 探针成功，但 `WECHAT_TOKEN` 是否轮换及是否与公众号后台 Token 一致尚未确认；客服消息真实用户路径和手机送达也尚未验证。

## 试运行前置条件

RAG 版本已部署并通过启动与本地 HTTP smoke。一次本机签名合成文字回调走通检索、生成和引用格式化，但来源摘录未清楚支持答案，不能据此认定引用可靠。此前合成 OpenID 的客服消息尝试返回 `errcode=48001`；最近一次直接 access-token API 探针成功，说明 AppID/AppSecret 与 API 调用来源可获取 access_token，但客服消息真实用户路径仍需实测。`WECHAT_TOKEN` 是否轮换及后台是否匹配尚未知。公网 `/wechat` 在用户浏览器中返回 `invalid signature`，表明无签名请求到达应用但未通过验签，不等于微信后台验证成功；公网 `/health` 返回 404 是预期路由边界，Nginx 只反代 `/wechat`，`/healthz` 仅供本机检查。实际手机消息送达仍需由用户通过微信客户端触发并确认。修改 unit 或任一 `.env` 后，使用仓库根目录 `sudo ./restart-wechat-dev-test.sh`，确认脚本报告 RAG 初始化成功且 health check 通过。

## 评估范围与机器快照

初筛范围为 3 至 5 份可公开、现行文档和约 10 个合成问题。检查文档抽取保真、证据命中、答案正确性与引用准确性、无答案时能否拒答，并分别记录解析、embedding、检索和生成的延迟与成本。这个小样本只用于 PoC 初筛。

机器资源快照是本轮读取 `/proc` 时的一次性观察：4 个 vCPU，`MemTotal` 约 7.75 GiB，`MemAvailable` 约 1.27 GiB，Swap 为 0。GPU 未核实。这不是恒定配置；按本次观察，首轮更适合调用远程 LLM API，但这不表示本地模型绝对不可能。

## 下一步

下一步建立固定标注的相关/无关 eval 集，核对答案与来源、评估检索阈值和模型自然拒答，再记录抽取、embedding、检索与生成的延迟和成本。离线 PoC 评估与真实微信试运行是两项独立工作。
