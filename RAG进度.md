# RAG PoC 进度

## 当前状态

公众号回调代码已接入 LlamaIndex `RagResponder`：文字消息在返回 `success` 后经远程 embedding/chat API 查询本地索引，再通过微信客服 API 发送带来源的回答；OpenID 不传给模型。若 RAG 初始化失败，服务仍启动以提供 health 和非文字回调，但文字回调返回 HTTP 503 且不确认；查询失败则发送通用回复，不记录 provider 异常。

旧版 RAG systemd unit 此前已 daemon-reload 并重启，使用根 `.env` 和 `rag-poc/.env`。用户曾报告旧服务 active、RAG 初始化成功、`/healthz` 返回 ok。最近一次直接调用微信 access-token API 成功，返回有效期 7200 秒；这证明当时 AppID/AppSecret 与 API 调用来源可获取 access_token，不代表客服消息已送达。用户报告已轮换公众号 AppSecret；旧 systemd 回滚用的 `WECHAT_TOKEN` 是否与公众号后台一致尚未确认。用户浏览器访问旧公网 `/wechat` 得到 `invalid signature`，与请求到达应用但未通过签名校验相符，不代表微信后台验证成功；旧公网 `/health` 返回 404 是因为 Nginx 只反代 `/wechat`，旧服务健康端点 `/healthz` 仅供本机检查。用户已确认所需数据处理授权及 provider 数据区域/留存条款；合成 live provider 探针通过。一次本机签名合成文字回调得到 `success`，RAG 生成了带引用编号的回答，但所列摘录未清楚支持答案，引用相关性仍需评估；此前合成 OpenID 的客服消息尝试返回 `errcode=48001`，未证明手机送达。

## 微信云托管消息推送

代码已适配微信云托管「消息推送」：用户最新反馈已在云托管启用该推送，当前接入方式为云内链路投递到 `/wechat`，不以默认公网域名作为公众号回调入口。代码仅在 `WECHAT_CLOUD_PUSH_ENABLED=true` 且请求带非空 `x-wx-source` 时进入云推送 JSON/XML 分支，否则会尝试公众号签名校验。当前只检查请求头是否存在，不验证其值或来源；因此云托管服务应关闭公网访问。用户反馈的是控制台配置状态，本助手未独立核验云端配置、真实请求头值或手机送达。

本地回归测试 19/19 通过。本地 Docker smoke 已验证镜像内 `import rag_poc`、`/healthz`、云托管路径检测、普通签名 GET echo，以及看板关闭后的 404；使用占位 provider endpoint 实例化 `RagResponder()` 也成功，证明当前索引能被镜像加载，但不代表真实 provider 网络可用。镜像构建使用锁定的 `rag-poc/uv.lock`，当前 Docker 构建工具固定为 uv 0.10.12。

云端当前配置应使用服务端口 80、`WECHAT_CLOUD_PUSH_ENABLED=true`、`WECHAT_OPEN_API_ENABLED=true`、`DASHBOARD_ENABLED=false`，试运行期间最小和最大副本均设为 1，并关闭服务公网访问。云托管消息推送选择当前环境与服务，路径设为 `/wechat`，格式建议 JSON；`CheckContainerPath` 检测必须返回 HTTP 200 和 `success`。若返回非 200（例如 403 或 503），检查请求是否进入当前代码的云推送分支，并检查服务可用性；控制台实际返回码尚未核实。确认扫码如涉及资源复用，必须由目标公众号管理员扫码。客服消息通过开放接口服务发送，仍需确认云调用白名单含 `/cgi-bin/message/custom/send`。这些是运行配置要求，不代表助手已观察到控制台实际值或真实用户消息送达。

当前回调会先快速返回 `success`，再通过 `BackgroundTasks` 执行 RAG 和客服消息发送。因为 RAG 实测可能超过微信约 5 秒回调窗口，不改为同步等待；云托管试运行保持最小副本 1、最大副本 1。这仍不是持久队列，实例崩溃、发布或网络故障可能丢失回复；多副本和正式可靠投递前需要持久化状态及 worker/队列。

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

实时 RAG 会将真实读者问题正文和检索到的馆藏片段发送到运行环境配置的远程 embedding/chat provider。云托管从服务环境变量读取 provider 设置，旧 systemd 服务使用 `rag-poc/.env`；OpenID 仅用于微信客服消息 API。用户已确认数据处理授权及 provider 数据区域/留存条款；答案事实、引用、拒答能力和 `min_score=0.2` 仍未系统评测。

云托管消息推送由用户报告已启用，真实云端回调与 RAG 到手机的完整链路尚无助手独立验证。旧 systemd 服务与签名 XML 是回滚路径。最近一次直接 access-token API 探针成功只说明当时 AppID/AppSecret 与 API 调用来源可获取 access_token；此前合成客服消息曾返回 `errcode=48001`，不证明云托管开放接口服务下的客服 API 权限或真实送达状态。

## 云托管试运行状态

当前按云托管消息推送路线试运行。控制台路径检测应返回 HTTP 200 和 `success`；非 200（例如 403 或 503）表示检测未通过。用户尚未报告检测的实际返回码，也未报告真实文字消息端到端结果，因此客服消息权限、答案质量或手机送达仍未验证。

云托管默认公网域名不是公众号消息推送入口，只供运维人员主动访问服务接口，例如检查 `/healthz`。关闭公网访问后无法通过该域名检查，不要为此重新开放公网，否则 `/wechat` 也会暴露。当前应用只检查 `x-wx-source` 是否存在，不验证请求头的值或来源；应用日志不记录该头，无法据现有日志确认真实值。用户报告已切换到云托管消息推送，但真实推送中的该头值尚未核实。核对时须在公网访问关闭的情况下临时加入受控诊断记录，触发一次真实推送，确认后移除诊断记录，再精确校验实测值或限制可信内网来源，并测试拒绝任意请求头。仅匹配一个可被公网伪造的固定字符串，不足以重新开放公网访问。

如解绑云托管消息推送，公众号原服务器 URL 不会自动恢复；需先在公众号后台停用服务器配置，再重新启用。资源复用时，消息推送配置确认的扫码需要目标公众号管理员完成。

## 评估范围与机器快照

初筛范围为 3 至 5 份可公开、现行文档和约 10 个合成问题。检查文档抽取保真、证据命中、答案正确性与引用准确性、无答案时能否拒答，并分别记录解析、embedding、检索和生成的延迟与成本。这个小样本只用于 PoC 初筛。

机器资源快照是本轮读取 `/proc` 时的一次性观察：4 个 vCPU，`MemTotal` 约 7.75 GiB，`MemAvailable` 约 1.27 GiB，Swap 为 0。GPU 未核实。这不是恒定配置；按本次观察，首轮更适合调用远程 LLM API，但这不表示本地模型绝对不可能。

## 下一步

下一步在公网访问关闭的情况下受控确认真实推送的 `x-wx-source` 值，移除临时诊断记录并收紧来源校验，再验证控制台路径检测与真实用户消息链路。另建立固定标注的相关/无关 eval 集，核对答案与来源、评估检索阈值和模型自然拒答，再记录抽取、embedding、检索与生成的延迟和成本。离线 PoC 评估与真实微信试运行是两项独立工作。
