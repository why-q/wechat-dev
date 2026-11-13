# 离线馆藏 RAG PoC

`rag-poc/` 是独立的离线试验目录，不属于微信回调服务，不会读取微信凭据或调用微信 API。PoC 只面向获准使用的公开馆藏文档和合成问题；不要放入真实读者消息、OpenID 或个人信息。公开文档片段和合成问题会发送到你配置的远程模型 API；provider 条款尚未核实，真实微信接入前必须重新评估数据边界。

框架采用 LlamaIndex：默认本地 `SimpleVectorStore`，并用 LlamaIndex 的 storage context 保存向量、docstore 和 index store。PDF、TXT、Markdown 由文件 reader 抽取可读取文本；不承诺扫描件/OCR。索引和凭据不应提交到版本库。

首次运行 `index` 或 `query` 时，PoC 会把 LlamaIndex 随包提供的 NLTK stopwords 与 Punkt 资源复制到 `rag-poc/.cache/nltk_data/`。这是项目私有、权限收紧的本地缓存：文件只复制一次、普通文件使用 `0600`、目录使用 `0700`；不下载 NLTK 数据，也不关闭 NLTK 的路径安全检查。之后索引和查询共用该缓存。

## 安装与配置

要求 Python 3.11+、[uv](https://docs.astral.sh/uv/)。所有环境与依赖都在本目录；不要在根项目运行安装命令。

```bash
cd rag-poc
uv sync --locked
cp .env.example .env
```

从当前 `rag-poc/` 目录编辑 `.env`，对应仓库根目录下的 `rag-poc/.env`；此文件被本目录的 `.gitignore` 忽略。配置以下六项。LLM 和 embedding endpoint、key、model 可来自不同 provider；不要复用 `WECHAT_TOKEN`、微信 AppID 或 Secret，也不要把这些值放入根 `.env`。

| 环境变量 | 含义 |
| --- | --- |
| `RAG_LLM_API_BASE` | 聊天 LLM 的 OpenAI-compatible API base URL，通常形如 `https://provider.example/v1` |
| `RAG_LLM_API_KEY` | 聊天 LLM API key；无鉴权服务也需填非空占位值 |
| `RAG_LLM_MODEL` | 聊天模型 ID |
| `RAG_EMBEDDING_API_BASE` | embedding 的 OpenAI-compatible API base URL；可与 LLM 不同 |
| `RAG_EMBEDDING_API_KEY` | embedding API key；无鉴权服务也需填非空占位值 |
| `RAG_EMBEDDING_MODEL` | embedding 模型 ID |

兼容范围是 OpenAI 风格的 chat completions 与 embeddings API；base URL 应为不含凭据、query 或 fragment 的 `http(s)` URL。Chat 仅使用普通聊天，不使用 function calling；provider 的实际兼容性需凭据配置后验证。index 命令只需要 embedding 三项；query 命令需要全部六项。`.env.example` 仅含示例值，不能直接用于真实索引或查询。

### 快速检查配置

默认的 `check-config` 只做本地检查，复用项目现有配置校验，确认六项环境变量非空，并确认两个 API base URL 是不含凭据、query 或 fragment 的 `http(s)` URL：

```bash
uv run --locked python -m rag_poc.cli check-config
```

`--only-config` 可显式写出同一条离线路径：

```bash
uv run --locked python -m rag_poc.cli check-config --only-config
```

本地格式检查不会发网络请求，因此不能证明 key 有效、model 存在或 provider endpoint 可用；成功只表示配置格式和必填项通过。它不会输出 key 或完整 API URL。

需要实际验证连通性时才使用 `--live`：

```bash
uv run --locked python -m rag_poc.cli check-config --live
```

此模式会把固定的合成 chat 与 embedding 探针发送到配置的两个远程 API，会产生网络流量，也可能产生 provider 费用。探针不含真实读者消息、OpenID 或个人信息，不调用微信；请求使用现有 OpenAI-like adapters，单次超时约 10 秒且显式不重试。index 与 query 不传该参数，保留 adapter 默认重试。命令独立报告 chat 和 embedding 通过项；任一失败均以非零状态退出，并只给出不含 SDK 异常、响应正文、请求头、key 或完整 URL 的可行动提示。成功输出只包含 provider 主机名、model 摘要和通过项。

## 添加资料、生成索引和提问

从仓库根目录看，语料目录是 `rag-poc/corpus/`；从下面命令所在的 `rag-poc/` 目录看，应把少量公开且可解析文本的 `.pdf`、`.txt` 或 `.md` 文件放入 `corpus/`：

```bash
uv run --locked python -m rag_poc.cli index
uv run --locked python -m rag_poc.cli query "合成问题：开放时间是什么？"
```

默认语料目录为 `corpus/`，持久索引目录为 `index/`。索引命令在本机解析文档并向远程 embedding endpoint 发送文本块；查询先向远程 embedding endpoint 发送问题检索，再在有足够证据时调用远程 chat LLM。命令不会读取微信消息，也不会触发微信外呼。

默认分块大小 `512` token、重叠 `80` token；默认检索 `top-k=4`，低于余弦相似度阈值 `0.2` 时拒答。可用 `--chunk-size`、`--chunk-overlap` 调整索引分块；可用 `--top-k`、`--min-score` 调整查询检索。相似度阈值只是未校准的初始值，应结合资料和合成问题评测后调整。添加或更换文档后需重新运行 index 命令。

例如：

```bash
uv run --locked python -m rag_poc.cli index --chunk-size 512 --chunk-overlap 80
uv run --locked python -m rag_poc.cli query "合成问题" --top-k 4 --min-score 0.2
```

回答仅在检索到达到阈值的来源时调用 LLM。LlamaIndex 的 `CitationQueryEngine.synthesize()` 会将这批已筛选的节点拆成带 `Source n:` 编号的 citation chunks，再交给回答生成器，不会再次检索。CLI 只从返回的 `source_nodes` 读取 metadata 和对应的本地 chunk 文本，核对 chunk 编号后用相同序号输出来源；每个来源包含语料相对路径，以及折叠空白并按 Unicode 字符截断到约 40 字符的原文摘录，不由模型生成。编号缺失、不一致或 chunk 内容为空时会拒绝输出回答和来源。来源会尽可能列出 PDF 页码或章节字段。模型生成的链接会从回答中移除。页码/章节是否可用取决于文件 reader 的抽取结果。阈值拒答是检索门槛，不保证模型不会误答；每条答案仍须人工核对原文。

CLI 帮助、空语料、本地配置格式错误和缺配置路径均可在不调用远程 API 的情况下检查：

```bash
uv run --locked python -m rag_poc.cli --help
uv run --locked python -m rag_poc.cli check-config --only-config
uv run --locked python -m rag_poc.cli index
```

index 命令在语料为空时以非零退出；配置缺失、索引不存在/损坏、文件抽取失败和 API 调用失败也会以非零退出。`check-config --live` 的任一远程探针失败同样以非零退出。为避免凭据泄露，异常不会打印底层 API error、请求头或 endpoint。

## 测试

```bash
uv run --locked python -m unittest discover -s tests -v
```

本轮已使用当前配置和语料中的 4 份 Markdown 文档，实际完成远程 embedding 索引和 chat 查询。三次合成问题分别询问服务项目、借阅期限/逾期规定和附近火星基地。前两个相关问题均以退出码 0 返回回答和 4 个 corpus 内来源，耗时分别为 16.682 秒、10.631 秒；无关的火星基地问题也以退出码 0 返回回答和 4 个 corpus 内来源，耗时 8.177 秒，没有输出 `ABSTENTION_TEXT`。检索探针显示该无关问题过了当前 `0.2` 门槛，因此多发生了一次 LLM 生成调用；这一次模型回答经脱敏分类为明确表示资料不足，没有虚构火星基地。此观察只针对这一次回答，不是通用安全保证，也不表示检索门槛已拒答。

三条问题形成的 `0.329364–0.628630` 只是经验分隔区间，不是校准阈值；当前默认值保持 `0.2`。后续需在至少 5 条无关问题的固定标注 eval 集中评估阈值表现，目前没有这组 eval 数据，也未完成校准。尚未系统评测回答事实、引用准确性或拒答能力；provider 服务条款仍未核实。

新增本地来源摘录后，使用当前索引再次运行相关问题查询，退出码为 0，返回 4 个来源，均包含从对应 citation chunk 提取的摘录。回答和摘录正文未记入此文档。
