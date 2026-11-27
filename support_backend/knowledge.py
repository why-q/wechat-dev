"""Tier 2 knowledge base builder and citation-verified RAG responder for `0.` corpus docs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .presets import split_frontmatter

DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_TOP_K = 4
DEFAULT_MIN_SCORE = 0.2
DEFAULT_LONG_ANSWER_CHARS = 220
DEFAULT_RAG_TIMEOUT_SECONDS = 25.0

_HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RAW_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


class KnowledgeError(RuntimeError):
    """Safe domain error for Tier 2 indexing or RAG querying without leaking credentials."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True, slots=True)
class KnowledgeApiConfig:
    embedding_api_base: str
    embedding_api_key: str
    embedding_model: str
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkPreview:
    """Plain-Python representation of a `0.` corpus chunk with citation metadata."""

    text: str
    doc_title: str
    file_name: str
    source_url: str
    header_path: str
    section_title: str

    def metadata_dict(self) -> dict[str, str]:
        return {
            "doc_title": self.doc_title,
            "file_name": self.file_name,
            "source": self.source_url,
            "header_path": self.header_path,
            "section_title": self.section_title,
        }


@dataclass(frozen=True, slots=True)
class Tier2Result:
    """Structured result emitted by Tier 2 public-source search."""

    outcome: str  # answered | need_clarify | staff_action | unsupported
    reply_text: str = ""
    short_conclusion: str = ""
    is_long_formatted: bool = False
    source_url: str = ""
    section_title: str = ""
    header_path: str = ""
    clarify_question: str = ""
    staff_reason: str = ""


def strip_model_urls(text: str) -> str:
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", text)
    return _RAW_URL_RE.sub("[链接已核对省略]", cleaned).strip()


def list_tier2_source_files(corpus_dir: Path) -> list[Path]:
    """Return strictly `0.`-prefixed Markdown/text files inside `corpus_dir`."""
    corpus_path = Path(corpus_dir)
    if not corpus_path.is_dir():
        raise KnowledgeError("corpus_missing", f"语料目录不存在：{corpus_path}")
    files = sorted(
        path
        for path in corpus_path.iterdir()
        if path.is_file() and path.name.startswith("0.") and path.suffix.lower() in {".md", ".txt"}
    )
    if not files:
        raise KnowledgeError("corpus_empty", f"语料目录中没有 `0.` 开头的公开资料：{corpus_path}")
    return files


def _complete_header_path(ancestor_path: str, current_heading: str) -> str:
    base = ancestor_path.strip() or "/"
    if not base.startswith("/"):
        base = "/" + base
    if not base.endswith("/"):
        base = base + "/"
    if not current_heading:
        return base
    return f"{base}{current_heading}/"


def _extract_heading_and_path(section_text: str, ancestor_path: str, fallback_title: str) -> tuple[str, str]:
    first_line = section_text.lstrip().splitlines()[0] if section_text.strip() else ""
    match = _HEADING_LINE_RE.match(first_line)
    if match is not None:
        current = match.group(1).strip()
        return current, _complete_header_path(ancestor_path, current)
    clean_fallback = fallback_title.strip() or "正文"
    return clean_fallback, _complete_header_path(ancestor_path, "")


def _split_paragraphs_bounded(text: str, max_chars: int = 1200, overlap_chars: int = 150) -> list[str]:
    """Fallback paragraph splitter matching `\n\n` boundaries when LlamaIndex is unavailable."""
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n+", text.strip()) if p.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap_chars:].strip() if overlap_chars > 0 and len(current) > overlap_chars else ""
            current = f"{tail}\n\n{para}".strip() if tail and len(para) < max_chars else para
        else:
            current = para
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            step = max(1, max_chars - overlap_chars)
            current = current[step:].strip()
    if current:
        chunks.append(current)
    return chunks


def _chunk_sections_pure_python(body: str, *, doc_title: str, file_name: str, source_url: str) -> list[ChunkPreview]:
    """Fallback Markdown heading + paragraph chunker matching design semantics without LlamaIndex."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    current_ancestor = "/"
    buffer: list[str] = []

    def flush() -> None:
        joined = "\n".join(buffer).strip()
        if joined:
            sections.append((current_ancestor, joined))

    for line in body.splitlines():
        m = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if m is not None:
            flush()
            buffer.clear()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            current_ancestor = "/" + "/".join(t for _, t in stack) + ("/" if stack else "")
            stack.append((level, title))
            buffer.append(line)
        else:
            buffer.append(line)
    flush()

    previews: list[ChunkPreview] = []
    for ancestor, section_text in sections:
        sec_title, full_path = _extract_heading_and_path(section_text, ancestor, doc_title)
        for piece in _split_paragraphs_bounded(section_text):
            previews.append(
                ChunkPreview(
                    text=piece,
                    doc_title=doc_title,
                    file_name=file_name,
                    source_url=source_url,
                    header_path=full_path,
                    section_title=sec_title,
                )
            )
    return previews


def build_chunks_from_corpus(
    corpus_dir: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    require_llamaindex: bool = False,
) -> list[ChunkPreview]:
    """Parse ONLY `0.` corpus docs: extract YAML `source`, strip frontmatter, split by section & `\n\n`."""
    files = list_tier2_source_files(corpus_dir)

    try:
        from llama_index.core import Document
        from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter

        _prepare_nltk_data_cache()
        md_parser = MarkdownNodeParser()
        para_splitter = SentenceSplitter(
            paragraph_separator="\n\n",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        has_llamaindex = True
    except ModuleNotFoundError:
        if require_llamaindex:
            raise KnowledgeError(
                "llamaindex_missing",
                "缺少 llama_index 依赖；请使用 rag-poc/.venv 环境运行 Tier 2 RAG。",
            ) from None
        has_llamaindex = False

    all_chunks: list[ChunkPreview] = []
    for file_path in files:
        raw = file_path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(raw)
        if not body:
            continue
        source_url = str(meta.get("source") or "").strip()
        raw_title = str(meta.get("title") or "").strip()
        doc_title = raw_title or file_path.stem.removeprefix("0.").strip() or file_path.stem

        if not has_llamaindex:
            all_chunks.extend(
                _chunk_sections_pure_python(
                    body, doc_title=doc_title, file_name=file_path.name, source_url=source_url
                )
            )
            continue

        doc = Document(text=body, metadata={"file_name": file_path.name, "doc_title": doc_title})
        section_nodes = md_parser.get_nodes_from_documents([doc])
        for section_node in section_nodes:
            section_text = section_node.get_content().strip()
            if not section_text:
                continue
            ancestor = str(section_node.metadata.get("header_path") or "/")
            sec_title, full_path = _extract_heading_and_path(section_text, ancestor, doc_title)
            sub_docs = [
                Document(
                    text=section_text,
                    metadata={
                        "doc_title": doc_title,
                        "file_name": file_path.name,
                        "source": source_url,
                        "header_path": full_path,
                        "section_title": sec_title,
                    },
                )
            ]
            split_nodes = para_splitter.get_nodes_from_documents(sub_docs, show_progress=False)
            for piece_node in split_nodes:
                piece_text = piece_node.get_content().strip()
                if not piece_text:
                    continue
                all_chunks.append(
                    ChunkPreview(
                        text=piece_text,
                        doc_title=doc_title,
                        file_name=file_path.name,
                        source_url=source_url,
                        header_path=full_path,
                        section_title=sec_title,
                    )
                )
    return all_chunks


def _validate_api_base(name: str, value: str) -> str:
    cleaned = value.strip()
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise KnowledgeError("rag_config_invalid", f"{name} URL 格式无效")
    parsed = urlsplit(cleaned)
    _ = parsed.port
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise KnowledgeError("rag_config_invalid", f"{name} URL 格式无效")
    return cleaned


def load_knowledge_api_config(env: Mapping[str, str], *, require_llm: bool) -> KnowledgeApiConfig:
    required = ["RAG_EMBEDDING_API_BASE", "RAG_EMBEDDING_API_KEY", "RAG_EMBEDDING_MODEL"]
    if require_llm:
        required.extend(["RAG_LLM_API_BASE", "RAG_LLM_API_KEY", "RAG_LLM_MODEL"])
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise KnowledgeError("rag_config_missing", "缺少 Tier 2 配置项：" + ", ".join(missing))

    embed_base = _validate_api_base("RAG_EMBEDDING_API_BASE", env["RAG_EMBEDDING_API_BASE"])
    llm_base = _validate_api_base("RAG_LLM_API_BASE", env["RAG_LLM_API_BASE"]) if require_llm and env.get("RAG_LLM_API_BASE", "").strip() else None
    return KnowledgeApiConfig(
        embedding_api_base=embed_base,
        embedding_api_key=env["RAG_EMBEDDING_API_KEY"].strip(),
        embedding_model=env["RAG_EMBEDDING_MODEL"].strip(),
        llm_api_base=llm_base,
        llm_api_key=env.get("RAG_LLM_API_KEY", "").strip() or None,
        llm_model=env.get("RAG_LLM_MODEL", "").strip() or None,
    )


def _prepare_nltk_data_cache(cache_dir: Path | None = None) -> None:
    """Ensure NLTK punkt/stopwords cache has single-link files owned by current euid."""
    from llama_index.core import utils as core_utils
    from nltk import data as nltk_data

    target_raw = os.environ.get("RAG_NLTK_DATA_DIR", "").strip()
    if cache_dir is not None:
        resolved_dir = Path(cache_dir)
    elif target_raw:
        resolved_dir = Path(target_raw)
    else:
        default_cache = Path(__file__).resolve().parents[1] / "rag-poc" / ".cache" / "nltk_data"
        resolved_dir = (
            default_cache
            if default_cache.exists() and default_cache.lstat().st_uid == os.geteuid()
            else Path(tempfile.gettempdir()) / f"wechat-support-nltk-{os.geteuid()}"
        )

    package_cache = Path(core_utils.__file__).resolve().parent / "_static" / "nltk_cache"
    resources = (Path("corpora/stopwords"), Path("tokenizers/punkt_tab"))

    needs_copy = not (resolved_dir / "corpora/stopwords/english").is_file() or not (
        resolved_dir / "tokenizers/punkt_tab/english"
    ).is_dir()
    if not needs_copy:
        info = resolved_dir.lstat()
        if info.st_uid != os.geteuid():
            raise KnowledgeError("nltk_cache_unowned", "NLTK 本地缓存目录属主与当前进程不一致")
    else:
        resolved_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".nltk-data-", dir=resolved_dir.parent))
        try:
            for rel in resources:
                src = package_cache / rel
                dst = stage / rel
                dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copytree(src, dst, symlinks=False, copy_function=shutil.copyfile)
            for item in stage.rglob("*"):
                item.chmod(0o700 if item.is_dir() else 0o600)
            stage.chmod(0o700)
            if not resolved_dir.exists():
                stage.rename(resolved_dir)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    cache_value = str(resolved_dir.resolve())
    os.environ["NLTK_DATA"] = cache_value
    nltk_data.path[:] = [p for p in nltk_data.path if str(Path(p).resolve()) != cache_value]
    nltk_data.path.insert(0, cache_value)


def _configure_nltk_if_present() -> None:
    try:
        _prepare_nltk_data_cache()
    except KnowledgeError:
        raise
    except Exception:
        pass


def build_tier2_index(
    *,
    corpus_dir: Path,
    index_dir: Path,
    env: Mapping[str, str] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[int, int]:
    """Build and persist the `0.`-only vector index using section + paragraph chunks."""
    env_map = os.environ if env is None else env
    config = load_knowledge_api_config(env_map, require_llm=False)
    _configure_nltk_if_present()
    previews = build_chunks_from_corpus(
        corpus_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        require_llamaindex=True,
    )
    if not previews:
        raise KnowledgeError("corpus_empty", "未从 `0.` 资料生成任何文本块")

    from llama_index.core import StorageContext, VectorStoreIndex
    from llama_index.core.schema import TextNode
    from llama_index.core.vector_stores import SimpleVectorStore
    from llama_index.embeddings.openai_like import OpenAILikeEmbedding

    embed_model = OpenAILikeEmbedding(
        model_name=config.embedding_model,
        api_base=config.embedding_api_base,
        api_key=config.embedding_api_key,
        timeout=DEFAULT_RAG_TIMEOUT_SECONDS,
        max_retries=0,
    )
    nodes = [
        TextNode(
            text=item.text,
            metadata=item.metadata_dict(),
            excluded_embed_metadata_keys=["source", "header_path"],
            excluded_llm_metadata_keys=["source", "file_name"],
        )
        for item in previews
    ]
    index_path = Path(index_dir)
    index_path.mkdir(parents=True, exist_ok=True)
    storage_context = StorageContext.from_defaults(vector_store=SimpleVectorStore())
    index = VectorStoreIndex(
        nodes,
        storage_context=storage_context,
        embed_model=embed_model,
        show_progress=False,
    )
    index.storage_context.persist(persist_dir=str(index_path))
    doc_files = len({item.file_name for item in previews})
    return doc_files, len(nodes)


def format_grounded_answer(
    *,
    answer: str,
    short_conclusion: str,
    source_url: str,
    section_title: str,
    long_threshold: int = DEFAULT_LONG_ANSWER_CHARS,
) -> tuple[str, bool]:
    """Apply short/long length rule with exact citation footer from metadata."""
    clean_answer = strip_model_urls(answer)
    clean_short = strip_model_urls(short_conclusion) or clean_answer
    if len(clean_answer) <= long_threshold:
        return clean_answer, False

    if source_url and section_title:
        footer = f"具体请参考 {source_url} 下的「{section_title}」节。"
        return f"{clean_short}\n\n{footer}", True
    if source_url:
        return f"{clean_short}\n\n具体请参考 {source_url} 。", True
    return clean_short, True


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON root is not an object")
    return parsed


class Tier2KnowledgeResponder:
    """Retrieves from `0.`-only index and generates verified citation-grounded answers."""

    def __init__(
        self,
        *,
        index_dir: Path,
        env: Mapping[str, str] | None = None,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        long_threshold: int = DEFAULT_LONG_ANSWER_CHARS,
        timeout_seconds: float = DEFAULT_RAG_TIMEOUT_SECONDS,
    ) -> None:
        env_map = os.environ if env is None else env
        config = load_knowledge_api_config(env_map, require_llm=True)
        _configure_nltk_if_present()
        self.index_dir = Path(index_dir)
        self.top_k = top_k
        self.min_score = float(min_score)
        self.long_threshold = int(long_threshold)
        self._lock = threading.Lock()

        if not self.index_dir.is_dir() or not any(self.index_dir.iterdir()):
            raise KnowledgeError("index_missing", f"Tier 2 索引目录为空或不存在：{self.index_dir}")

        try:
            from llama_index.core import Settings, StorageContext, load_index_from_storage
            from llama_index.embeddings.openai_like import OpenAILikeEmbedding
            from llama_index.llms.openai_like import OpenAILike
        except ModuleNotFoundError:
            raise KnowledgeError("llamaindex_missing", "缺少 llama_index 依赖") from None

        embed_model = OpenAILikeEmbedding(
            model_name=config.embedding_model,
            api_base=config.embedding_api_base,
            api_key=config.embedding_api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        llm = OpenAILike(
            model=config.llm_model or "",
            api_base=config.llm_api_base,
            api_key=config.llm_api_key,
            is_chat_model=True,
            is_function_calling_model=False,
            timeout=timeout_seconds,
            max_retries=0,
        )
        Settings.embed_model = embed_model
        Settings.llm = llm
        storage_context = StorageContext.from_defaults(persist_dir=str(self.index_dir))
        index = load_index_from_storage(storage_context, embed_model=embed_model)
        self._retriever = index.as_retriever(similarity_top_k=self.top_k)
        self._llm = llm

    def generate_clarify_question(self, question: str, system_prompt: str) -> str:
        """Ask the configured chat model to phrase one concise clarifying question."""
        from llama_index.core.base.llms.types import ChatMessage, MessageRole

        with self._lock:
            resp = self._llm.chat(
                [
                    ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                    ChatMessage(role=MessageRole.USER, content=question),
                ]
            )
        return str(getattr(resp.message, "content", None) or resp)

    def query(
        self,
        question: str,
        *,
        clarification_context: str = "",
        allow_clarify: bool = True,
    ) -> Tier2Result:
        """Retrieve `0.` sources, judge sufficiency, and format short or long cited reply."""
        if not question.strip():
            return Tier2Result(outcome="unsupported")

        search_query = f"{clarification_context}\n{question}".strip() if clarification_context else question.strip()
        with self._lock:
            try:
                retrieved = self._retriever.retrieve(search_query)
            except Exception:
                raise KnowledgeError("embedding_retrieval_failed", "向量检索阶段失败") from None

            selected = [
                item
                for item in retrieved
                if getattr(item, "score", None) is not None and float(item.score) >= self.min_score
            ]
            if not selected:
                return Tier2Result(outcome="unsupported")

            context_blocks: list[str] = []
            for idx, item in enumerate(selected, start=1):
                node = getattr(item, "node", item)
                meta = getattr(node, "metadata", {}) or {}
                doc_title = str(meta.get("doc_title") or meta.get("file_name") or "公开资料")
                section = str(meta.get("section_title") or meta.get("header_path") or "正文")
                body = node.get_content().strip()
                context_blocks.append(f"Source [{idx}] ({doc_title} / {section}):\n{body}")

            prompt = self._build_synthesis_prompt(
                question=question.strip(),
                clarification_context=clarification_context.strip(),
                context_text="\n\n".join(context_blocks),
                source_count=len(selected),
                allow_clarify=allow_clarify,
            )
            try:
                from llama_index.core.base.llms.types import ChatMessage, MessageRole

                llm_response = self._llm.chat(
                    [ChatMessage(role=MessageRole.USER, content=prompt)]
                )
                raw_content = getattr(llm_response.message, "content", None) or str(llm_response)
            except Exception:
                raise KnowledgeError("llm_synthesis_failed", "公开资料归纳生成阶段失败") from None

        try:
            parsed = _extract_json_object(str(raw_content))
        except Exception:
            raise KnowledgeError("llm_invalid_json", "生成结果不符合 JSON 结构") from None

        outcome = str(parsed.get("outcome") or "").strip().lower()
        if outcome not in {"answered", "need_clarify", "staff_action", "unsupported"}:
            return Tier2Result(outcome="unsupported")

        if outcome == "unsupported":
            return Tier2Result(outcome="unsupported")

        if outcome == "staff_action":
            reason = strip_model_urls(str(parsed.get("staff_reason") or "需馆员核实处理"))
            return Tier2Result(outcome="staff_action", staff_reason=reason)

        if outcome == "need_clarify":
            clarify_q = strip_model_urls(str(parsed.get("clarify_question") or ""))
            if not allow_clarify or not clarify_q:
                return Tier2Result(
                    outcome="staff_action",
                    staff_reason="澄清机会已用完或需人工确认" if not allow_clarify else "缺关键检索细节",
                )
            return Tier2Result(outcome="need_clarify", clarify_question=clarify_q)

        # `answered`: verify source_index points strictly to a retrieved node.
        idx_raw = parsed.get("source_index")
        if type(idx_raw) is not int or not (1 <= idx_raw <= len(selected)):
            return Tier2Result(outcome="unsupported")

        chosen_node = getattr(selected[idx_raw - 1], "node", selected[idx_raw - 1])
        chosen_meta = getattr(chosen_node, "metadata", {}) or {}
        source_url = str(chosen_meta.get("source") or "").strip()
        section_title = str(chosen_meta.get("section_title") or "").strip()
        header_path = str(chosen_meta.get("header_path") or "").strip()

        answer_text = str(parsed.get("answer") or "").strip()
        short_conclusion = str(parsed.get("short_conclusion") or "").strip()
        if not answer_text and not short_conclusion:
            return Tier2Result(outcome="unsupported")

        formatted, is_long = format_grounded_answer(
            answer=answer_text or short_conclusion,
            short_conclusion=short_conclusion or answer_text,
            source_url=source_url,
            section_title=section_title,
            long_threshold=self.long_threshold,
        )
        return Tier2Result(
            outcome="answered",
            reply_text=formatted,
            short_conclusion=strip_model_urls(short_conclusion or answer_text),
            is_long_formatted=is_long,
            source_url=source_url,
            section_title=section_title,
            header_path=header_path,
        )

    @staticmethod
    def _build_synthesis_prompt(
        *,
        question: str,
        clarification_context: str,
        context_text: str,
        source_count: int,
        allow_clarify: bool,
    ) -> str:
        clarify_rule = (
            "- If the sources show different rules (e.g. campus/role differences) and the reader hasn't specified which applies, set outcome='need_clarify' and ask one short specific question in clarify_question."
            if allow_clarify
            else "- Clarification was ALREADY attempted once. Do NOT choose 'need_clarify'. Either give a complete conditional answer covering both cases if supported, or choose 'staff_action'/'unsupported'."
        )
        extra_ctx = f"\nPrior clarification context:\n{clarification_context}\n" if clarification_context else ""
        return (
            "You are the Shantou University Library assistant. Rely STRICTLY on the provided Source texts below. "
            "Never invent library rules, phone numbers, URLs, or Markdown links.\n\n"
            f"Sources (valid indices 1..{source_count}):\n{context_text}\n\n"
            f"Reader Question:\n{question}\n{extra_ctx}\n"
            "Return ONE valid JSON object with fields:\n"
            "{\n"
            '  "outcome": "answered" | "need_clarify" | "staff_action" | "unsupported",\n'
            '  "source_index": integer (1..' + str(source_count) + ', required when outcome is "answered"),\n'
            '  "short_conclusion": "1-2 concise sentences directly stating the core conclusion in Chinese",\n'
            '  "answer": "clear complete grounded answer in Chinese without raw URLs or citations",\n'
            '  "clarify_question": "one concise Chinese clarifying question when outcome is need_clarify",\n'
            '  "staff_reason": "short Chinese reason when outcome is staff_action"\n'
            "}\n"
            "Rules:\n"
            "- Only choose outcome='answered' when the Sources explicitly support the complete answer.\n"
            f"{clarify_rule}\n"
            "- If the question requires checking personal borrowing status, waiving fines, handling active complaints, or fixing broken hardware/accounts, choose outcome='staff_action'.\n"
            "- If Sources do not support answering, choose outcome='unsupported'."
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="support-knowledge", description="Tier 2 0.-only corpus tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    inspect_p = sub.add_parser("inspect-chunks", help="Preview 0.-only chunks and citation metadata without API calls")
    inspect_p.add_argument("--corpus", type=Path, default=Path("rag-poc/corpus"))

    build_p = sub.add_parser("build-index", help="Build Tier 2 0.-only vector index")
    build_p.add_argument("--corpus", type=Path, default=Path("rag-poc/corpus"))
    build_p.add_argument("--index-dir", type=Path, default=Path("rag-poc/index_v2"))
    build_p.add_argument("--env-file", type=Path, default=None, help="Optional .env file to load before indexing")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cmd == "inspect-chunks":
        chunks = build_chunks_from_corpus(args.corpus)
        by_file: dict[str, int] = {}
        missing_source: list[str] = []
        for c in chunks:
            by_file[c.file_name] = by_file.get(c.file_name, 0) + 1
            if not c.source_url:
                missing_source.append(c.file_name)
        print(
            json.dumps(
                {
                    "chunk_count": len(chunks),
                    "files": by_file,
                    "missing_source_files": sorted(set(missing_source)),
                    "sample": [
                        {
                            "file_name": c.file_name,
                            "source_url": c.source_url,
                            "header_path": c.header_path,
                            "section_title": c.section_title,
                            "chars": len(c.text),
                        }
                        for c in chunks[:8]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "build-index":
        if args.env_file is not None and args.env_file.is_file():
            try:
                from dotenv import load_dotenv

                load_dotenv(args.env_file)
            except ModuleNotFoundError:
                pass
        files_n, chunks_n = build_tier2_index(corpus_dir=args.corpus, index_dir=args.index_dir)
        for item in Path(args.index_dir).rglob("*"):
            if item.is_file():
                item.chmod(0o644)
            elif item.is_dir():
                item.chmod(0o755)
        Path(args.index_dir).chmod(0o755)
        print(f"Built Tier 2 index: files={files_n} chunks={chunks_n} dir={args.index_dir}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
