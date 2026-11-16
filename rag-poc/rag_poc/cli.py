import argparse
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_DIR = ROOT_DIR / "corpus"
DEFAULT_NLTK_DATA_DIR = ROOT_DIR / ".cache" / "nltk_data"
DEFAULT_INDEX_DIR = ROOT_DIR / "index"
SUPPORTED_SUFFIXES = frozenset({".pdf", ".txt", ".md"})
DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_TOP_K = 4
DEFAULT_MIN_SCORE = 0.2
CONFIG_CHECK_TIMEOUT_SECONDS = 10.0
RAG_REQUEST_TIMEOUT_SECONDS = 30.0
CHAT_PROBE_TEXT = "Synthetic chat connectivity probe. Reply with one short word."
EMBEDDING_PROBE_TEXT = "Synthetic embedding connectivity probe."
ABSTENTION_TEXT = "馆藏资料中没有足够的可检索证据回答此问题。"


class PoCError(Exception):
    """Expected user-facing PoC errors, without provider details."""


def configure_nltk_data_cache(cache_dir: Path | None = None) -> None:
    if cache_dir is None:
        cache_dir = Path(os.environ.get("RAG_NLTK_DATA_DIR", DEFAULT_NLTK_DATA_DIR))
    from llama_index.core import utils as core_utils

    cache_dir = Path(cache_dir)
    cache_parent = cache_dir.parent
    package_cache = Path(core_utils.__file__).resolve().parent / "_static" / "nltk_cache"
    resources = (Path("corpora/stopwords"), Path("tokenizers/punkt_tab"))

    def secure_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PoCError("本地分词资源缓存目录不是当前用户拥有的普通目录。")
        path.chmod(0o700)

    def validate_tree(root: Path) -> None:
        for relative in resources:
            resource_root = root / relative
            directories = [root]
            directories.extend(
                root / Path(*relative.parts[:index])
                for index in range(1, len(relative.parts) + 1)
            )
            for directory in directories:
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                    raise PoCError("本地分词资源缓存包含不安全的目录。")
                directory.chmod(0o700)
            for item in resource_root.rglob("*"):
                info = item.lstat()
                if info.st_uid != os.geteuid():
                    raise PoCError("本地分词资源缓存包含非当前用户拥有的文件。")
                if stat.S_ISDIR(info.st_mode):
                    item.chmod(0o700)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    item.chmod(0o600)
                else:
                    raise PoCError("本地分词资源缓存包含不安全的文件。")

    try:
        secure_directory(cache_parent)
        if cache_dir.exists() or cache_dir.is_symlink():
            secure_directory(cache_dir)
            validate_tree(cache_dir)
        else:
            stage = Path(tempfile.mkdtemp(prefix=".nltk-data-", dir=cache_parent))
            try:
                for relative in resources:
                    source = package_cache / relative
                    if not source.is_dir() or source.is_symlink():
                        raise PoCError("LlamaIndex 安装中缺少所需的本地分词资源。")
                    target = stage / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.copytree(
                        source, target, symlinks=True, copy_function=shutil.copyfile
                    )
                validate_tree(stage)
                stage.rename(cache_dir)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

        cache_value = str(cache_dir.resolve())
        os.environ["NLTK_DATA"] = cache_value
        from nltk import data as nltk_data

        nltk_data.path[:] = [
            path
            for path in nltk_data.path
            if str(Path(path).resolve()) != cache_value
        ]
        nltk_data.path.insert(0, cache_value)
        for resource in ("corpora/stopwords", "tokenizers/punkt_tab"):
            nltk_data.find(resource, paths=[cache_value])

        from llama_index.core.utils import globals_helper

        globals_helper.wait_for_nltk_check()
    except PoCError:
        raise
    except Exception:
        raise PoCError(
            "无法准备安全的本地分词资源缓存；请检查 PoC 本地依赖和 "
            "rag-poc/.cache/nltk_data 权限。"
        ) from None

@dataclass(frozen=True)
class ApiConfig:
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    embedding_api_base: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None


def _validate_api_base(name: str, value: str) -> None:
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise PoCError(f"{name} 必须是有效的 http(s) OpenAI-compatible API base URL。")
    try:
        parts = urlsplit(value)
        # Accessing port forces urlsplit to reject malformed numeric ports.
        _ = parts.port
    except ValueError:
        raise PoCError(
            f"{name} 必须是有效的 http(s) OpenAI-compatible API base URL。"
        ) from None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PoCError(f"{name} 必须是有效的 http(s) OpenAI-compatible API base URL。")
    if (
        parts.username
        or parts.password
        or "@" in parts.netloc
        or "?" in value
        or "#" in value
    ):
        raise PoCError(
            f"{name} 不可包含用户名、密码、query 或 fragment；凭据应放在对应 API_KEY 变量。"
        )


def load_api_config(env: Mapping[str, str], *, require_llm: bool) -> ApiConfig:
    required = [
        "RAG_EMBEDDING_API_BASE",
        "RAG_EMBEDDING_API_KEY",
        "RAG_EMBEDDING_MODEL",
    ]
    if require_llm:
        required.extend(
            ["RAG_LLM_API_BASE", "RAG_LLM_API_KEY", "RAG_LLM_MODEL"]
        )
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise PoCError("缺少必填环境变量：" + ", ".join(missing))

    embedding_base = env["RAG_EMBEDDING_API_BASE"].strip()
    _validate_api_base("RAG_EMBEDDING_API_BASE", embedding_base)

    llm_base = env.get("RAG_LLM_API_BASE", "").strip() or None
    if require_llm and llm_base is not None:
        _validate_api_base("RAG_LLM_API_BASE", llm_base)

    return ApiConfig(
        llm_api_base=llm_base,
        llm_api_key=env.get("RAG_LLM_API_KEY", "").strip() or None,
        llm_model=env.get("RAG_LLM_MODEL", "").strip() or None,
        embedding_api_base=embedding_base,
        embedding_api_key=env["RAG_EMBEDDING_API_KEY"].strip(),
        embedding_model=env["RAG_EMBEDDING_MODEL"].strip(),
    )


def supported_corpus_files(corpus_dir: Path) -> list[Path]:
    if not corpus_dir.is_dir():
        raise PoCError(f"语料目录不存在或不是目录：{corpus_dir}")
    files = sorted(
        path
        for path in corpus_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        if any(path.is_file() for path in corpus_dir.rglob("*")):
            raise PoCError(
                f"语料目录没有受支持的文件；仅支持可抽取文本的 PDF、TXT、Markdown：{corpus_dir}"
            )
        raise PoCError(
            f"语料目录为空：{corpus_dir}。请先添加公开 PDF、TXT 或 Markdown 文档。"
        )
    return files


def validate_chunk_settings(chunk_size: int, chunk_overlap: int) -> None:
    if chunk_size <= 0:
        raise PoCError("--chunk-size 必须是正整数。")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise PoCError("--chunk-overlap 必须大于等于 0 且小于 --chunk-size。")


def qualifying_sources(source_nodes: Sequence[object], min_score: float) -> list[object]:
    return [
        source
        for source in source_nodes
        if getattr(source, "score", None) is not None
        and float(source.score) >= min_score
    ]


def synthesize_if_supported(query_engine, query_bundle, min_score: float):
    retrieved = query_engine.retrieve(query_bundle)
    selected = qualifying_sources(retrieved, min_score)
    if not selected:
        return None
    return query_engine.synthesize(query_bundle, selected)


def source_reference(number: int, metadata: Mapping[str, object], corpus_dir: Path) -> str:
    file_path = metadata.get("file_path")
    file_name = metadata.get("file_name")
    if file_path:
        source_path = Path(str(file_path))
        try:
            display_path = source_path.resolve().relative_to(corpus_dir.resolve())
        except (OSError, ValueError):
            display_path = Path(str(file_name)) if file_name else Path(source_path.name)
    elif file_name:
        display_path = Path(str(file_name))
    else:
        display_path = Path("unknown source")

    label = display_path.as_posix()
    page = metadata.get("page_label") or metadata.get("page_number") or metadata.get("page")
    if page is not None and str(page).strip():
        label += f" — 第 {page} 页"
    section = metadata.get("section") or metadata.get("heading") or metadata.get("header_path")
    if section is not None and str(section).strip():
        label += f" — {section}"
    return f"[{number}] {label}"


def citation_source_references(
    source_nodes: Sequence[object] | None,
    corpus_dir: Path,
) -> list[str]:
    if not source_nodes:
        raise PoCError(
            "CitationQueryEngine 未返回引用 source_nodes；拒绝输出无法核对的来源。"
        )

    from llama_index.core.schema import MetadataMode

    references = []
    for number, source_node in enumerate(source_nodes, start=1):
        node = getattr(source_node, "node", None)
        if node is None:
            raise PoCError(
                "CitationQueryEngine 返回的 source_nodes 编号与提示来源不一致；"
                "拒绝输出未核对引用。"
            )
        content = node.get_content(metadata_mode=MetadataMode.NONE)
        prefix = f"Source {number}:\n"
        if not content.startswith(prefix):
            raise PoCError(
                "CitationQueryEngine 返回的 source_nodes 编号与提示来源不一致；"
                "拒绝输出未核对引用。"
            )
        metadata = getattr(node, "metadata", None)
        if not isinstance(metadata, Mapping):
            raise PoCError("CitationQueryEngine 返回的来源缺少可核对 metadata。")
        excerpt = re.sub(r"\s+", " ", content[len(prefix):]).strip()
        if not excerpt:
            raise PoCError("CitationQueryEngine 返回的来源缺少可核对文本摘录。")
        if len(excerpt) > 40:
            excerpt = excerpt[:39].rstrip() + "…"
        references.append(f'{source_reference(number, metadata, corpus_dir)} — “{excerpt}”')
    return references


def strip_model_links(answer: str) -> str:
    answer = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", answer)
    return re.sub(
        r"(?:https?://|www\.)\S+", "[外部链接已省略]", answer, flags=re.IGNORECASE
    )


def _embedding_model(
    config: ApiConfig,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
):
    from llama_index.embeddings.openai_like import OpenAILikeEmbedding

    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return OpenAILikeEmbedding(
        model_name=config.embedding_model,
        api_base=config.embedding_api_base,
        api_key=config.embedding_api_key,
        **kwargs,
    )


def _llm_model(
    config: ApiConfig,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
):
    from llama_index.llms.openai_like import OpenAILike

    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return OpenAILike(
        model=config.llm_model,
        api_base=config.llm_api_base,
        api_key=config.llm_api_key,
        is_chat_model=True,
        is_function_calling_model=False,
        **kwargs,
    )


def _safe_summary_token(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    token = re.sub(r"[^A-Za-z0-9._:/@+-]", "_", value).strip("_")
    return token[:80] or fallback


def _safe_provider_name(api_base: str | None) -> str:
    if not api_base:
        return "未配置"
    try:
        hostname = urlsplit(api_base).hostname
    except ValueError:
        return "未知 provider"
    if not hostname:
        return "未知 provider"
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return "未知 provider"
    return _safe_summary_token(hostname, "未知 provider")


def _print_config_summary(config: ApiConfig) -> None:
    print("通过：六项环境变量齐全，API base URL 格式有效。")
    print(
        "配置摘要："
        f" chat provider={_safe_provider_name(config.llm_api_base)}"
        f" model={_safe_summary_token(config.llm_model, '未命名模型')}；"
        f" embedding provider={_safe_provider_name(config.embedding_api_base)}"
        f" model={_safe_summary_token(config.embedding_model, '未命名模型')}"
    )


def _probe_chat_endpoint(config: ApiConfig) -> None:
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    llm = _llm_model(
        config,
        timeout=CONFIG_CHECK_TIMEOUT_SECONDS,
        max_retries=0,
    )
    # This fixed synthetic message contains no reader content or identifiers.
    llm.chat([ChatMessage(role=MessageRole.USER, content=CHAT_PROBE_TEXT)])


def _probe_embedding_endpoint(config: ApiConfig) -> None:
    embedding = _embedding_model(
        config,
        timeout=CONFIG_CHECK_TIMEOUT_SECONDS,
        max_retries=0,
    )
    vector = embedding.get_text_embedding(EMBEDDING_PROBE_TEXT)
    if vector is None or len(vector) == 0:
        raise ValueError("empty embedding")


def check_config(*, live: bool = False) -> None:
    """Check all six settings, optionally sending fixed probes to both APIs."""
    config = load_api_config(os.environ, require_llm=True)
    _print_config_summary(config)
    if not live:
        print("通过：仅完成本地配置格式检查，未发送网络请求。")
        return

    print(
        "远程探针：将向上述配置的 API 发送固定合成文本；会产生网络流量，可能产生费用。"
    )
    failures: list[str] = []
    try:
        _probe_chat_endpoint(config)
    except Exception:
        failures.append(
            "chat endpoint 未通过；请检查 API base、model、key、网络和 provider 兼容性。"
        )
    else:
        print("通过：LLM chat endpoint 最小请求。")

    try:
        _probe_embedding_endpoint(config)
    except Exception:
        failures.append(
            "embedding endpoint 未通过；请检查 API base、model、key、网络和 provider 兼容性。"
        )
    else:
        print("通过：embedding endpoint 最小请求。")

    if failures:
        raise PoCError(
            "远程 API 探针未全部通过（未显示 SDK 异常、响应正文或请求凭据）：\n"
            + "\n".join(f"- {failure}" for failure in failures)
        )

def build_index(
    corpus_dir: Path,
    index_dir: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> None:
    validate_chunk_settings(chunk_size, chunk_overlap)
    files = supported_corpus_files(corpus_dir)
    config = load_api_config(os.environ, require_llm=False)
    configure_nltk_data_cache()

    try:
        from llama_index.core import SimpleDirectoryReader, StorageContext, VectorStoreIndex
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.core.vector_stores import SimpleVectorStore

        documents = SimpleDirectoryReader(
            input_files=[str(path) for path in files],
            filename_as_id=True,
        ).load_data()
        if not documents:
            raise PoCError("文档读取器没有从语料文件中提取到文本。")

        nodes = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        ).get_nodes_from_documents(documents, show_progress=False)
        if not nodes:
            raise PoCError("文档未生成可索引文本块；请检查是否含可抽取文本。")

        storage_context = StorageContext.from_defaults(vector_store=SimpleVectorStore())
        index = VectorStoreIndex(
            nodes,
            storage_context=storage_context,
            embed_model=_embedding_model(config),
            show_progress=False,
        )
        index.storage_context.persist(persist_dir=str(index_dir))
    except PoCError:
        raise
    except Exception:
        raise PoCError(
            "索引生成失败（文档抽取或 embedding API 请求失败）。未显示底层异常或 endpoint；"
            "请检查文档、API base/model/key、网络及 provider 兼容性。"
        ) from None

    print(f"索引已保存：{len(files)} 份文档，{len(nodes)} 个文本块；目录 {index_dir}")


def load_index(index_dir: Path, embedding_model):
    if not index_dir.is_dir() or not any(index_dir.iterdir()):
        raise PoCError(
            f"索引不存在：{index_dir}。请先运行 index 命令生成本地索引。"
        )
    try:
        from llama_index.core import StorageContext, load_index_from_storage

        storage_context = StorageContext.from_defaults(persist_dir=str(index_dir))
        return load_index_from_storage(
            storage_context,
            embed_model=embedding_model,
        )
    except Exception:
        raise PoCError(
            f"索引无法读取或不完整：{index_dir}。请重新运行 index 命令；未显示底层异常。"
        ) from None


class RagResponder:
    def __init__(
        self,
        index_dir: Path | None = None,
        corpus_dir: Path | None = None,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        if top_k <= 0:
            raise PoCError("--top-k 必须是正整数。")
        if not 0 <= min_score <= 1:
            raise PoCError("--min-score 必须在 0 到 1 之间。")
        self.index_dir = Path(
            index_dir if index_dir is not None else os.environ.get("RAG_INDEX_DIR", DEFAULT_INDEX_DIR)
        )
        self.corpus_dir = Path(
            corpus_dir if corpus_dir is not None else os.environ.get("RAG_CORPUS_DIR", DEFAULT_CORPUS_DIR)
        )
        self.min_score = min_score
        self._query_lock = threading.Lock()
        config = load_api_config(os.environ, require_llm=True)
        configure_nltk_data_cache()
        try:
            from llama_index.core import Settings
            from llama_index.core.prompts import PromptTemplate
            from llama_index.core.query_engine import CitationQueryEngine

            embedding = _embedding_model(
                config, timeout=RAG_REQUEST_TIMEOUT_SECONDS, max_retries=0
            )
            llm = _llm_model(
                config, timeout=RAG_REQUEST_TIMEOUT_SECONDS, max_retries=0
            )
            Settings.embed_model = embedding
            Settings.llm = llm
            index = load_index(self.index_dir, embedding)
            self.query_engine = CitationQueryEngine.from_args(
                index,
                llm=llm,
                similarity_top_k=top_k,
                citation_chunk_size=DEFAULT_CHUNK_SIZE,
                citation_chunk_overlap=DEFAULT_CHUNK_OVERLAP,
                citation_qa_template=PromptTemplate(
                    "Answer only from the provided source text. Do not use prior knowledge. "
                    "If the sources do not contain enough evidence, say so plainly. "
                    "Cite supported claims with the provided source numbers in [n] form. "
                    "Never invent or output URLs or links.\n\n"
                    "Sources:\n{context_str}\n\nQuestion: {query_str}\nAnswer: "
                ),
                citation_refine_template=PromptTemplate(
                    "Answer only from the provided source text and do not use prior knowledge. "
                    "If the sources do not contain enough evidence, say so plainly. "
                    "Cite supported claims with the provided source numbers in [n] form. "
                    "Never invent or output URLs or links.\n"
                    "Question: {query_str}\nExisting answer: {existing_answer}\n"
                    "Additional numbered sources:\n{context_msg}\n"
                    "Refine only when the new sources support it; otherwise retain the answer.\n"
                    "Answer: "
                ),
            )
        except PoCError:
            raise
        except Exception:
            raise PoCError(
                "RAG 服务初始化失败（索引或本地依赖不可用）；未显示底层异常。"
            ) from None

    def answer(self, question: str) -> str:
        if not question.strip():
            raise PoCError("问题不能为空。")
        try:
            from llama_index.core.schema import QueryBundle

            with self._query_lock:
                response = synthesize_if_supported(
                    self.query_engine, QueryBundle(question), self.min_score
                )
            if response is None:
                return ABSTENTION_TEXT
            answer = strip_model_links(str(response).strip())
            if not answer:
                raise PoCError("LLM 返回空回答；没有可显示的答案。")
            references = citation_source_references(
                getattr(response, "source_nodes", None), self.corpus_dir
            )
            return answer + "\n\n可核对来源：\n" + "\n".join(references)
        except PoCError:
            raise
        except Exception:
            raise PoCError(
                "查询失败（embedding/LLM API 请求失败，或索引不可用）。未显示底层异常或 endpoint。"
            ) from None


def query_index(
    question: str,
    index_dir: Path,
    corpus_dir: Path,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> None:
    responder = RagResponder(
        index_dir,
        corpus_dir,
        top_k=top_k,
        min_score=min_score,
    )
    print(responder.answer(question))



def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("请输入正整数。") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("请输入正整数。")
    return parsed


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-poc",
        description="使用本地馆藏文档与 OpenAI-compatible 远程 API 的离线 RAG PoC。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser(
        "check-config", help="检查六项配置格式；使用 --live 才请求远程 API"
    )
    config_mode = config_parser.add_mutually_exclusive_group()
    config_mode.add_argument(
        "--live",
        action="store_true",
        help="发送固定合成 chat/embedding 探针到配置的远程 API",
    )
    config_mode.add_argument(
        "--only-config",
        action="store_true",
        help="仅做本地配置格式检查（默认行为）",
    )

    index_parser = subparsers.add_parser("index", help="读取 PDF/TXT/Markdown 并离线持久化向量索引")
    index_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    index_parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX_DIR)
    index_parser.add_argument("--chunk-size", type=_positive_int, default=DEFAULT_CHUNK_SIZE)
    index_parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)

    query_parser = subparsers.add_parser("query", help="检索本地索引并回答问题")
    query_parser.add_argument("question", help="合成问题；不要提供真实读者消息、OpenID 或个人信息")
    query_parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX_DIR)
    query_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    query_parser.add_argument("--top-k", type=_positive_int, default=DEFAULT_TOP_K)
    query_parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    args = create_parser().parse_args(argv)
    load_dotenv(ROOT_DIR / ".env", override=False)
    try:
        if args.command == "check-config":
            check_config(live=args.live)
        elif args.command == "index":
            build_index(
                args.corpus,
                args.index_dir,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
        else:
            query_index(
                args.question,
                args.index_dir,
                args.corpus,
                top_k=args.top_k,
                min_score=args.min_score,
            )
    except PoCError as exc:
        print(f"rag-poc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
