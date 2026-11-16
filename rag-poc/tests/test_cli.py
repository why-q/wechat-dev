import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llama_index.core.schema import NodeWithScore, TextNode

class _LoopbackProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(raw_body),
            }
        )
        if self.server.status_code != 200:
            response = b'{"error":{"message":"sensitive provider body"}}'
            self.send_response(self.server.status_code)
        elif self.path.endswith("/chat/completions"):
            response = json.dumps(
                {
                    "id": "local-chat",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "chat-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
        elif self.path.endswith("/embeddings"):
            response = json.dumps(
                {
                    "object": "list",
                    "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
                    "model": "embed-model",
                }
            ).encode()
            self.send_response(200)
        else:
            response = b'{"error":{"message":"unexpected path"}}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        return


@contextlib.contextmanager
def _loopback_probe_server(status_code=200):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackProbeHandler)
    server.daemon_threads = True
    server.requests = []
    server.status_code = status_code
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

from rag_poc.cli import (
    ABSTENTION_TEXT,
    CHAT_PROBE_TEXT,
    CONFIG_CHECK_TIMEOUT_SECONDS,
    DEFAULT_MIN_SCORE,
    EMBEDDING_PROBE_TEXT,
    RAG_REQUEST_TIMEOUT_SECONDS,
    ApiConfig,
    PoCError,
    RagResponder,
    _embedding_model,
    _llm_model,
    _probe_chat_endpoint,
    _probe_embedding_endpoint,
    citation_source_references,
    configure_nltk_data_cache,
    load_api_config,
    main,
    qualifying_sources,
    source_reference,
    strip_model_links,
    supported_corpus_files,
    synthesize_if_supported,
    validate_chunk_settings,
)


class ConfigurationTests(unittest.TestCase):
    def test_missing_embedding_settings_are_named_without_values(self):
        with self.assertRaises(PoCError) as caught:
            load_api_config({"RAG_EMBEDDING_API_KEY": "private-value"}, require_llm=False)

        message = str(caught.exception)
        self.assertIn("RAG_EMBEDDING_API_BASE", message)
        self.assertIn("RAG_EMBEDDING_MODEL", message)
        self.assertNotIn("private-value", message)

    def test_chat_and_embedding_settings_are_independent(self):
        values = {
            "RAG_LLM_API_BASE": "https://chat.example/v1",
            "RAG_LLM_API_KEY": "chat-key",
            "RAG_LLM_MODEL": "chat-model",
            "RAG_EMBEDDING_API_BASE": "https://embed.example/v1",
            "RAG_EMBEDDING_API_KEY": "embed-key",
            "RAG_EMBEDDING_MODEL": "embed-model",
        }
        config = load_api_config(values, require_llm=True)

        self.assertEqual(config.llm_api_base, "https://chat.example/v1")
        self.assertEqual(config.embedding_api_base, "https://embed.example/v1")
        self.assertEqual(config.embedding_model, "embed-model")

    def test_api_base_rejects_credentials_in_url_without_echoing_value(self):
        values = {
            "RAG_EMBEDDING_API_BASE": "https://user:private-value@embed.example/v1",
            "RAG_EMBEDDING_API_KEY": "key",
            "RAG_EMBEDDING_MODEL": "model",
        }
        with self.assertRaises(PoCError) as caught:
            load_api_config(values, require_llm=False)
        self.assertNotIn("private-value", str(caught.exception))

    def test_malformed_api_base_is_reported_without_echoing_value(self):
        values = {
            "RAG_EMBEDDING_API_BASE": "https://[private-value/v1",
            "RAG_EMBEDDING_API_KEY": "key",
            "RAG_EMBEDDING_MODEL": "model",
        }
        with self.assertRaises(PoCError) as caught:
            load_api_config(values, require_llm=False)
        self.assertNotIn("private-value", str(caught.exception))

    def test_check_config_default_is_local_and_redacted(self):
        values = {
            "RAG_LLM_API_BASE": "https://chat.example/v1",
            "RAG_LLM_API_KEY": "chat-secret",
            "RAG_LLM_MODEL": "chat-model",
            "RAG_EMBEDDING_API_BASE": "https://embed.example/v1",
            "RAG_EMBEDDING_API_KEY": "embedding-secret",
            "RAG_EMBEDDING_MODEL": "embed-model",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("rag_poc.cli.load_dotenv"), patch.dict(os.environ, values, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main(["check-config"])

        self.assertEqual(status, 0)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn("仅完成本地配置格式检查，未发送网络请求", output)
        self.assertIn("chat.example", output)
        self.assertIn("chat-model", output)
        self.assertNotIn("chat-secret", output)
        self.assertNotIn("embedding-secret", output)
        self.assertNotIn("https://chat.example/v1", output)
        self.assertNotIn("https://embed.example/v1", output)

    def test_check_config_only_config_reports_missing_settings(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("rag_poc.cli.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main(["check-config", "--only-config"])

        self.assertEqual(status, 1)
        self.assertIn("RAG_EMBEDDING_API_BASE", stderr.getvalue())
        self.assertIn("RAG_LLM_API_BASE", stderr.getvalue())
        self.assertNotIn("private", stdout.getvalue() + stderr.getvalue())

    def _sample_api_config(self) -> ApiConfig:
        return ApiConfig(
            llm_api_base="https://chat.example/v1",
            llm_api_key="chat-secret",
            llm_model="chat-model",
            embedding_api_base="https://embed.example/v1",
            embedding_api_key="embedding-secret",
            embedding_model="embed-model",
        )

    def test_default_factories_omit_retry_and_timeout_overrides(self):
        config = self._sample_api_config()
        with (
            patch("llama_index.llms.openai_like.OpenAILike") as llm,
            patch("llama_index.embeddings.openai_like.OpenAILikeEmbedding") as embedding,
        ):
            _llm_model(config)
            _embedding_model(config)

        self.assertNotIn("max_retries", llm.call_args.kwargs)
        self.assertNotIn("timeout", llm.call_args.kwargs)
        self.assertNotIn("max_retries", embedding.call_args.kwargs)
        self.assertNotIn("timeout", embedding.call_args.kwargs)

    def test_probes_disable_retries_without_calling_network(self):
        config = self._sample_api_config()
        with (
            patch("rag_poc.cli._llm_model") as llm_factory,
            patch("rag_poc.cli._embedding_model") as embedding_factory,
        ):
            llm_factory.return_value.chat.return_value = object()
            embedding_factory.return_value.get_text_embedding.return_value = [0.0]
            _probe_chat_endpoint(config)
            _probe_embedding_endpoint(config)

        llm_factory.assert_called_once_with(
            config,
            timeout=CONFIG_CHECK_TIMEOUT_SECONDS,
            max_retries=0,
        )
        embedding_factory.assert_called_once_with(
            config,
            timeout=CONFIG_CHECK_TIMEOUT_SECONDS,
            max_retries=0,
        )
        chat_messages = llm_factory.return_value.chat.call_args.args[0]
        self.assertEqual(chat_messages[0].content, "Synthetic chat connectivity probe. Reply with one short word.")
        embedding_factory.return_value.get_text_embedding.assert_called_once_with(
            "Synthetic embedding connectivity probe."
        )

    def _live_environment(self, server):
        base = f"http://127.0.0.1:{server.server_port}/v1"
        return {
            "RAG_LLM_API_BASE": base,
            "RAG_LLM_API_KEY": "loopback-chat-secret",
            "RAG_LLM_MODEL": "chat-model",
            "RAG_EMBEDDING_API_BASE": base,
            "RAG_EMBEDDING_API_KEY": "loopback-embedding-secret",
            "RAG_EMBEDDING_MODEL": "embed-model",
        }, base

    def _run_live_with_environment(self, values):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("rag_poc.cli.load_dotenv"), patch.dict(os.environ, values, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main(["check-config", "--live"])
        return status, stdout.getvalue() + stderr.getvalue()

    def test_live_check_uses_real_adapters_against_loopback_server(self):
        with _loopback_probe_server() as server:
            values, full_base = self._live_environment(server)
            status, output = self._run_live_with_environment(values)

            self.assertEqual(status, 0)
            self.assertIn("通过：LLM chat endpoint 最小请求。", output)
            self.assertIn("通过：embedding endpoint 最小请求。", output)
            self.assertNotIn(values["RAG_LLM_API_KEY"], output)
            self.assertNotIn(values["RAG_EMBEDDING_API_KEY"], output)
            self.assertNotIn(full_base, output)

            requests_by_path = {request["path"]: request for request in server.requests}
            self.assertEqual(
                set(requests_by_path),
                {"/v1/chat/completions", "/v1/embeddings"},
            )
            chat_request = requests_by_path["/v1/chat/completions"]
            chat_headers = {key.lower(): value for key, value in chat_request["headers"].items()}
            self.assertEqual(
                chat_headers["authorization"],
                f"Bearer {values['RAG_LLM_API_KEY']}",
            )
            self.assertEqual(chat_request["body"]["model"], "chat-model")
            self.assertEqual(chat_request["body"]["messages"][0]["content"], CHAT_PROBE_TEXT)

            embedding_request = requests_by_path["/v1/embeddings"]
            embedding_headers = {
                key.lower(): value for key, value in embedding_request["headers"].items()
            }
            self.assertEqual(
                embedding_headers["authorization"],
                f"Bearer {values['RAG_EMBEDDING_API_KEY']}",
            )
            embedding_input = embedding_request["body"]["input"]
            if isinstance(embedding_input, list):
                self.assertEqual(embedding_input, [EMBEDDING_PROBE_TEXT])
            else:
                self.assertEqual(embedding_input, EMBEDDING_PROBE_TEXT)

    def test_live_check_401_is_nonzero_and_redacted(self):
        with _loopback_probe_server(status_code=401) as server:
            values, full_base = self._live_environment(server)
            status, output = self._run_live_with_environment(values)

            self.assertEqual(status, 1)
            self.assertIn("chat endpoint 未通过", output)
            self.assertIn("embedding endpoint 未通过", output)
            self.assertNotIn(values["RAG_LLM_API_KEY"], output)
            self.assertNotIn(values["RAG_EMBEDDING_API_KEY"], output)
            self.assertNotIn(full_base, output)
            self.assertNotIn("sensitive provider body", output)
            self.assertEqual(
                {request["path"] for request in server.requests},
                {"/v1/chat/completions", "/v1/embeddings"},
            )


class OfflineHelperTests(unittest.TestCase):

    def test_rag_responder_uses_bounded_provider_requests(self):
        config = ApiConfig(
            llm_api_base="https://chat.example/v1",
            llm_api_key="chat-secret",
            llm_model="chat-model",
            embedding_api_base="https://embed.example/v1",
            embedding_api_key="embedding-secret",
            embedding_model="embed-model",
        )
        with (
            patch("rag_poc.cli.load_api_config", return_value=config),
            patch("llama_index.core.Settings", new=SimpleNamespace(embed_model=None, llm=None)),
            patch("rag_poc.cli.configure_nltk_data_cache"),
            patch("rag_poc.cli._embedding_model") as embedding_factory,
            patch("rag_poc.cli._llm_model") as llm_factory,
            patch("rag_poc.cli.load_index", return_value=object()),
            patch(
                "llama_index.core.query_engine.CitationQueryEngine.from_args",
                return_value=object(),
            ),
        ):
            RagResponder(index_dir=Path("unused-index"), corpus_dir=Path("unused-corpus"))

        embedding_factory.assert_called_once_with(
            config, timeout=30.0, max_retries=0
        )
        llm_factory.assert_called_once_with(
            config, timeout=30.0, max_retries=0
        )

    def test_rag_responder_serializes_model_queries(self):
        responder = object.__new__(RagResponder)
        responder.query_engine = object()
        responder.min_score = DEFAULT_MIN_SCORE
        responder.corpus_dir = Path("unused-corpus")
        responder._query_lock = threading.Lock()
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        active = 0
        max_active = 0
        calls = 0
        guard = threading.Lock()
        answers = []
        errors = []

        def synthesize(*_args):
            nonlocal active, max_active, calls
            with guard:
                calls += 1
                call = calls
                active += 1
                max_active = max(max_active, active)
            if call == 1:
                first_entered.set()
                release_first.wait(2)
            else:
                second_entered.set()
            with guard:
                active -= 1
            return None

        def answer():
            try:
                answers.append(responder.answer("合成问题"))
            except Exception as error:
                errors.append(error)

        with patch("rag_poc.cli.synthesize_if_supported", side_effect=synthesize):
            first = threading.Thread(target=answer)
            second = threading.Thread(target=answer)
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            self.assertFalse(second_entered.wait(0.1))
            release_first.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(answers, [ABSTENTION_TEXT, ABSTENTION_TEXT])
        self.assertEqual(max_active, 1)
    def test_empty_or_unsupported_corpus_fails_before_any_api_setup(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            with self.assertRaisesRegex(PoCError, "语料目录为空"):
                supported_corpus_files(corpus)

            (corpus / "scan.png").write_bytes(b"not a supported corpus file")
            with self.assertRaises(PoCError) as caught:
                supported_corpus_files(corpus)
            self.assertIn("没有受支持的文件", str(caught.exception))

    def test_supported_files_are_pdf_text_or_markdown(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            pdf = corpus / "book.PDF"
            markdown = corpus / "guide.md"
            unsupported = corpus / "image.png"
            for path in (pdf, markdown, unsupported):
                path.write_bytes(b"local test fixture")

            self.assertEqual(
                supported_corpus_files(corpus),
                [pdf, markdown],
            )

    def test_chunk_overlap_must_be_smaller_than_chunk(self):
        validate_chunk_settings(512, 80)
        with self.assertRaisesRegex(PoCError, "--chunk-overlap"):
            validate_chunk_settings(128, 128)

    def test_private_nltk_cache_allows_sentence_splitter_and_reuses_copy(self):
        from llama_index.core import Document
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.core.utils import globals_helper
        from nltk import data as nltk_data

        prior_env = os.environ.get("NLTK_DATA")
        prior_nltk_paths = list(nltk_data.path)
        prior_helper_state = (
            globals_helper._nltk_data_dir,
            globals_helper._stopwords,
            globals_helper._punkt_tokenizer,
        )
        try:
            with tempfile.TemporaryDirectory() as temporary_dir:
                cache = Path(temporary_dir) / "nltk_data"
                configure_nltk_data_cache(cache)
                english_dir = cache / "corpora" / "stopwords" / "english"
                tokenizer_dir = cache / "tokenizers" / "punkt_tab" / "english"
                files = [
                    path
                    for root in (english_dir, tokenizer_dir)
                    for path in root.rglob("*")
                    if path.is_file()
                ]
                self.assertTrue(files)
                self.assertEqual({path.stat().st_nlink for path in files}, {1})
                self.assertEqual({path.stat().st_mode & 0o777 for path in files}, {0o600})
                self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
                first_inode = files[0].stat().st_ino

                nodes = SentenceSplitter(
                    chunk_size=128, chunk_overlap=0
                ).get_nodes_from_documents(
                    [Document(text="The library opens at nine. It closes at five.")],
                    show_progress=False,
                )
                self.assertTrue(nodes)
                self.assertEqual(
                    " ".join(node.text for node in nodes),
                    "The library opens at nine. It closes at five.",
                )

                configure_nltk_data_cache(cache)
                self.assertEqual(files[0].stat().st_ino, first_inode)
        finally:
            nltk_data.path[:] = prior_nltk_paths
            (
                globals_helper._nltk_data_dir,
                globals_helper._stopwords,
                globals_helper._punkt_tokenizer,
            ) = prior_helper_state
            if prior_env is None:
                os.environ.pop("NLTK_DATA", None)
            else:
                os.environ["NLTK_DATA"] = prior_env

    def test_similarity_gate_abstains_when_evidence_is_below_threshold(self):
        hits = [SimpleNamespace(score=0.19), SimpleNamespace(score=0.08)]
        self.assertEqual(qualifying_sources(hits, DEFAULT_MIN_SCORE), [])
        self.assertEqual(qualifying_sources(hits, 0.1), hits[:1])

    def test_source_reference_uses_corpus_relative_path_and_available_page(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            metadata = {
                "file_path": str(corpus / "rules" / "hours.pdf"),
                "file_name": "hours.pdf",
                "page_label": "4",
            }
            self.assertEqual(
                source_reference(2, metadata, corpus),
                "[2] rules/hours.pdf — 第 4 页",
            )

    def test_citation_numbers_follow_returned_source_nodes_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            source_nodes = [
                NodeWithScore(
                    node=TextNode(
                        text="Source 1:\nThe library opens at nine.",
                        metadata={
                            "file_path": str(corpus / "rules" / "hours.pdf"),
                            "file_name": "hours.pdf",
                            "page_label": "4",
                        },
                    ),
                    score=0.8,
                ),
                NodeWithScore(
                    node=TextNode(
                        text="Source 2:\nHours are listed here.",
                        metadata={
                            "file_path": str(corpus / "guide.md"),
                            "file_name": "guide.md",
                            "heading": "Hours",
                        },
                    ),
                    score=0.7,
                ),
            ]

            self.assertEqual(
                citation_source_references(source_nodes, corpus),
                [
                    '[1] rules/hours.pdf — 第 4 页 — “The library opens at nine.”',
                    '[2] guide.md — Hours — “Hours are listed here.”',
                ],
            )

            mismatched = NodeWithScore(
                node=TextNode(text="Source 2:\nWrong numbering", metadata={}),
                score=0.5,
            )
            with self.assertRaisesRegex(PoCError, "编号"):
                citation_source_references([mismatched], corpus)

    def test_same_file_chunk_citations_include_distinct_truncated_excerpts(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            source_path = corpus / "guide.md"
            source_nodes = [
                NodeWithScore(
                    node=TextNode(
                        text="Source 1:\nFirst chunk covers opening hours.",
                        metadata={"file_path": str(source_path), "file_name": "guide.md"},
                    ),
                    score=0.8,
                ),
                NodeWithScore(
                    node=TextNode(
                        text="Source 2:\nSecond chunk covers overdue notices.",
                        metadata={"file_path": str(source_path), "file_name": "guide.md"},
                    ),
                    score=0.7,
                ),
            ]

            references = citation_source_references(source_nodes, corpus)

            self.assertEqual(
                references,
                [
                    '[1] guide.md — “First chunk covers opening hours.”',
                    '[2] guide.md — “Second chunk covers overdue notices.”',
                ],
            )
            self.assertNotEqual(
                references[0].split(" — “", 1)[1],
                references[1].split(" — “", 1)[1],
            )

    def test_empty_or_invalid_citation_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            metadata = {"file_path": str(corpus / "guide.md"), "file_name": "guide.md"}
            empty = NodeWithScore(
                node=TextNode(text="Source 1:\n  \t\n", metadata=metadata), score=0.8
            )
            with self.assertRaisesRegex(PoCError, "文本摘录"):
                citation_source_references([empty], corpus)

            invalid_prefix = NodeWithScore(
                node=TextNode(text="Source 2:\nSome source text", metadata=metadata), score=0.8
            )
            with self.assertRaisesRegex(PoCError, "编号"):
                citation_source_references([invalid_prefix], corpus)


    def test_citation_excerpt_is_truncated_by_unicode_character_count(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            corpus = Path(temporary_dir)
            excerpt = "甲" * 45
            node = NodeWithScore(
                node=TextNode(
                    text=f"Source 1:\n{excerpt}",
                    metadata={"file_path": str(corpus / "guide.md")},
                ),
                score=0.8,
            )

            reference = citation_source_references([node], corpus)[0]

            rendered_excerpt = reference.split(" — “", 1)[1][:-1]
            self.assertEqual(len(rendered_excerpt), 40)
            self.assertTrue(rendered_excerpt.endswith("…"))

    def test_low_score_path_retrieves_once_and_skips_synthesis(self):
        class RecordingQueryEngine:
            def __init__(self, scores):
                self.nodes = [SimpleNamespace(score=score) for score in scores]
                self.retrieve_calls = 0
                self.synthesize_calls = 0

            def retrieve(self, query_bundle):
                self.retrieve_calls += 1
                return self.nodes

            def synthesize(self, query_bundle, nodes):
                self.synthesize_calls += 1
                self.synthesized_nodes = nodes
                return object()

        query = object()
        below_threshold = RecordingQueryEngine([0.19, 0.08])
        self.assertIsNone(
            synthesize_if_supported(below_threshold, query, DEFAULT_MIN_SCORE)
        )
        self.assertEqual(below_threshold.retrieve_calls, 1)
        self.assertEqual(below_threshold.synthesize_calls, 0)

        sufficient = RecordingQueryEngine([0.8, 0.1])
        self.assertIsNotNone(
            synthesize_if_supported(sufficient, query, DEFAULT_MIN_SCORE)
        )
        self.assertEqual(sufficient.retrieve_calls, 1)
        self.assertEqual(sufficient.synthesize_calls, 1)
        self.assertEqual(sufficient.synthesized_nodes, sufficient.nodes[:1])

    def test_model_urls_are_removed_from_answer(self):
        answer = "详情见 [馆藏页面](https://example.org/page) 或 https://example.org/raw"
        sanitized = strip_model_links(answer)
        self.assertNotIn("https://", sanitized)
        self.assertIn("馆藏页面", sanitized)
        self.assertIn("[外部链接已省略]", sanitized)
        self.assertNotIn("www.example", strip_model_links("[页面](www.example.com)"))


if __name__ == "__main__":
    unittest.main()
