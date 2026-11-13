# RAG PoC 进度

## 当前状态

用户已决定使用 LlamaIndex，独立离线 PoC 目录现已创建。线上 FastAPI 微信回调仍没有接入 LLM 或 RAG。

当前服务仍是 FastAPI 微信测试号回调：文字消息快速 ACK，等待 10 秒后发送固定测试回复，没有接入 LLM 或 RAG。用户在新版 `sendcustommessage` 实现和服务重启后报告固定回复测试已成功，这是较新的用户报告，不是助手独立复验；旧项目背景文档中的未验证状态早于该报告，尚未同步。当前 `pyproject.toml` 只有 defusedxml、FastAPI 和 Uvicorn。

现有服务继续以 `DynamicUser`、`ProtectSystem=strict`、`ProtectHome=yes` 运行，工作目录为 `/srv/wechat-dev-test`，只读绑定 `app.py` 和根 `.venv`，没有绑定 `rag-poc`。PoC 已放在独立 `rag-poc/`，使用自己的 `.venv` 和依赖锁文件，不修改生产 service，也不接收真实微信回调。

## PoC 已创建

已新增 `rag-poc/pyproject.toml`、`rag-poc/uv.lock`、独立 `.venv`、`rag-poc/rag_poc/cli.py`、README 和本地测试。依赖使用 LlamaIndex core、file reader、OpenAI-compatible LLM/embedding adapter 与 dotenv；仅安装在 PoC `.venv`。

`rag-poc/corpus/` 已创建，当前有用户添加的 4 份 Markdown 馆藏文档。索引命令支持可抽取文本的 PDF、TXT 和 Markdown，以 `SimpleVectorStore` 建索引，并通过 LlamaIndex storage context 持久化向量、docstore 与 index store 到默认 `rag-poc/index/`。每个 chunk 保留 reader 提供的文件和可用页码/章节 metadata。

问题命令使用 LlamaIndex `CitationQueryEngine`，普通 chat 模式关闭 function calling。检索后先按 `min_score` 筛证据，未达门槛时输出固定拒答文案且不调用生成；达标时调用模型，但模型也可能自行拒答。来源序号会与 `source_nodes` 校验，引用列出语料相对路径和可用页码/章节，并附从对应 citation chunk 本文提取、折叠空白后截断到最多 40 个 Unicode 字符的摘录，不由模型生成。当前 `min_score=0.2` 尚未经过系统评测校准，答案与引用质量也未系统评测。

`.env.example` 和 README 已列出所需的六项 PoC 专用变量：`RAG_LLM_API_BASE`、`RAG_LLM_API_KEY`、`RAG_LLM_MODEL`、`RAG_EMBEDDING_API_BASE`、`RAG_EMBEDDING_API_KEY`、`RAG_EMBEDDING_MODEL`。索引只需 embedding 三项，查询需要全部六项。PoC 自己的 `.env`、`.venv`、`.cache/` 和 `index/` 已由 `rag-poc/.gitignore` 忽略。没有改动根 `pyproject.toml`、根 `.venv`、根 `.env`、`app.py`、service 或 Nginx。

已定位分词失败原因：锁定环境中的 NLTK `pathsec.py` `_hardened_open()` 在 `st_nlink > 1` 时拒绝打开文件（第 2238 至 2243 行）。LlamaIndex 随包的 stopwords 34 个文件和 punkt_tab 77 个文件均为 `nlink=2`、权限 `0644`、属主 root，因此触发安全拒绝。用临时私有目录准备独立资源后，`wait_for_nltk_check()` 和 Punkt tokenizer 均成功，资源文件 link count 全为 1；没有下载数据或改动 site-packages。

PoC 现在会在 index/query 初始化 SentenceSplitter 前，把 LlamaIndex bundled stopwords 和 punkt_tab 复制到 `rag-poc/.cache/nltk_data/`，后续复用副本。目录权限设为 `0700`，文件权限设为 `0600`，并检查属主、文件类型与 link count；不关闭 NLTK 安全 guard，也不联网下载 NLTK 数据。新增测试会实际运行 SentenceSplitter，并验证缓存重复初始化时不会重复制。

PoC 独立 `.venv` 中 `uv sync --locked` 成功。来源摘录修复后的最终代码运行 22 项 unittest，全部通过（`uv run --locked python -m unittest discover -s tests -v`，2.205 秒）。本地分块 smoke 生成 1 个 chunk。真实索引命令处理语料中的 4 份 Markdown，生成 34 个文本块并保存向量、docstore 和 index store。索引后和加入来源摘录后都运行过真实相关 query；最近一次退出码为 0，耗时 17.591 秒，返回 4 个来源，均含对应 citation chunk 摘录。实际调用使用配置的远程 embedding 与 chat API，未调用微信接口；回答与摘录正文未写入此记录。测试集本身不调用远程 provider API。

此前一次 `check-config --live` 曾观察到 embedding 探针通过、chat 探针未通过；之后实际 index 与 query 均成功。另对服务项目、规章制度借阅期限/逾期、火星基地三个合成问题各运行一次真实查询，均以退出码 0 完成，耗时分别为 16.682 秒、10.631 秒和 8.177 秒，每次返回 4 个 corpus 内来源。只检索不生成的探针对每题取回 4 个节点：服务项目分数最低为 0.628630，最高为 0.654950；规章制度问题最低为 0.673374，最高为 0.716506；火星无关问题最低为 0.312753，最高为 0.329364。当前 `min_score=0.2` 时，这三组观测节点均过门槛。本次小样本的经验分隔区间为 `0.329364 < min_score <= 0.628630`，但它不是经校准的通用阈值，本轮未调整阈值。

本次火星无关问题的检索节点全部超过当前 `min_score=0.2`，因此系统继续调用 LLM 生成回答。对这一次实际回答的脱敏分类是“明确拒答/资料不足”，未虚构火星基地，也没有输出固定 `ABSTENTION_TEXT`。这次观察到的失败模式是多发生了一次 LLM 生成调用，而不是观察到编造答案；这只描述本次样本，不构成通用安全保证。检索门槛拒答和模型自然拒答是两条不同路径。回答正文未写入记录。答案事实、引用和拒答能力仍未系统评测，也未测量成本。

已新增 `check-config` 子命令。默认路径和显式的 `--only-config` 只复用 `load_api_config(require_llm=True)` 做六项变量齐全与 API base URL 格式检查，不发网络请求，也不能证明 key、model 或远程 endpoint 有效；`--live` 才使用现有 OpenAI-like adapters 向 chat 与 embedding endpoint 各发送一次固定合成探针。探针不含真实读者消息、OpenID 或个人信息，设置约 10 秒超时且显式 `max_retries=0`；index 与 query 不传该参数，保留 adapter 默认重试。探针会产生网络流量并可能产生费用。命令只输出 provider 主机名、model 摘要和通过项，失败时不输出 SDK 异常、响应正文、请求头、key 或完整 URL。

用户可从 `rag-poc/` 运行 `uv run --locked python -m rag_poc.cli check-config`，或用 `check-config --only-config` 显式表示离线路径；缺配置或格式错误以非零状态退出。只有在确认允许产生远程流量和可能费用、并已提供真实凭据时才运行 `check-config --live`；两类探针均通过才返回零状态。永久测试仅把 `--live` 指向 127.0.0.1 临时 server，不连接真实 provider，也不会调用微信或发送真实读者数据。

## 尚未确定

用户此前报告已配置六项 `RAG_LLM_*` 和 `RAG_EMBEDDING_*` 变量，并向 `rag-poc/corpus/` 添加 4 个 Markdown 文档。本轮未读取凭据或文档正文；实际索引与查询均已成功。三条问题得到的 `0.329364–0.628630` 只是经验间隔，不是校准阈值；默认值保持 `0.2`。后续在至少 5 条无关问题组成的固定标注 eval 集阶段评估阈值表现，届时再考虑校准，不代表目前已有这组问题或已完成校准。答案事实、引用准确度和无答案问题的拒答能力尚未系统评测，provider 服务条款仍未核实。

当前批准的离线文档 PoC 使用公开资料和合成问题，调用已配置的远程 LLM 与 embedding API，暂不接入微信消息。这是本轮验证范围，不是隐私结论。公开片段和合成问题会发送到已配置的远程 API，provider 的数据处理条款尚未核实；按用户决定，隐私评估暂不阻塞本轮工作。用户提出的“当前读者消息不包含个人信息”只是工作假设，不构成真实消息外发授权。

### 生产接入前重新评估

在把真实读者消息正文、检索片段或 OpenID 发送给外部 LLM/embedding API，或将 RAG 接入实时微信之前，必须重新评估并明确数据处理边界，包括 provider 区域与留存、日志和消息保存、OpenID 处理，以及审批和权限。当前离线 PoC 外发公开片段和合成问题仍按用户决定不做隐私评估，也不作为 blocker；这一工作假设不等于批准发送真实消息。

## 评估范围与机器快照

初筛范围为 3 至 5 份可公开、现行文档和约 10 个合成问题。检查文档抽取保真、证据命中、答案正确性与引用准确性、无答案时能否拒答，并分别记录解析、embedding、检索和生成的延迟与成本。这个小样本只用于 PoC 初筛。

机器资源快照是本轮读取 `/proc` 时的一次性观察：4 个 vCPU，`MemTotal` 约 7.75 GiB，`MemAvailable` 约 1.27 GiB，Swap 为 0。GPU 未核实。这不是恒定配置；按本次观察，首轮更适合调用远程 LLM API，但这不表示本地模型绝对不可能。

## 下一步

下一步建立固定标注的相关/无关 eval 集，核对答案与来源、评估检索阈值和模型自然拒答，再记录抽取、embedding、检索与生成的延迟和成本。当前 `min_score` 保持 `0.2`。真实微信消息、OpenID 和外呼仍不属于当前 PoC。
