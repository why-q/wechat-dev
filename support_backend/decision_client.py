"""HTTP JSON client for `/v1/systemone` decision models (`decision-model-preview`)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as URLRequest, urlopen


DEFAULT_DECISION_MODEL = "decision-model-preview"
DEFAULT_DECISION_TIMEOUT_SECONDS = 3.0


class DecisionClientError(RuntimeError):
    """Safe error raised when a `/v1/systemone` decision request fails or times out."""

    def __init__(self, reason_code: str, *, status_code: int | None = None) -> None:
        self.reason_code = reason_code
        self.status_code = status_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    """Normalized answer returned by one question inside `/v1/systemone`."""

    choice: str | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    probability: float | None = None
    score: float | None = None


def build_systemone_url(base_url: str) -> str:
    """Normalize a site root (or /v1 URL) into a validated `.../v1/systemone` URL."""
    cleaned = base_url.strip()
    if not cleaned:
        raise ValueError("Decision base URL must not be empty")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ValueError("Decision base URL contains invalid whitespace or control characters")
    parsed = urlsplit(cleaned)
    _ = parsed.port
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Decision base URL must use http or https with a valid host")
    if parsed.username or parsed.password or "@" in parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Decision base URL must not contain credentials, query, or fragment")

    path = parsed.path.rstrip("/")
    if path.endswith("/v1/systemone"):
        target_path = path
    elif path.endswith("/v1"):
        target_path = f"{path}/systemone"
    elif path:
        target_path = f"{path}/v1/systemone"
    else:
        target_path = "/v1/systemone"
    return urlunsplit((parsed.scheme, parsed.netloc, target_path, "", ""))


def _coerce_float(raw: object) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _parse_prob_map(raw: object) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, float] = {}
    for k, v in raw.items():
        val = _coerce_float(v)
        if isinstance(k, str) and val is not None:
            parsed[k] = val
    return parsed


def _parse_answer(raw: object) -> DecisionAnswer:
    if not isinstance(raw, dict):
        if isinstance(raw, str):
            return DecisionAnswer(choice=raw)
        return DecisionAnswer()
    choice_val = raw.get("choice")
    if choice_val is None and isinstance(raw.get("answer"), str):
        choice_val = raw.get("answer")
    if choice_val is None and isinstance(raw.get("label"), str):
        choice_val = raw.get("label")
    return DecisionAnswer(
        choice=str(choice_val) if isinstance(choice_val, str) else None,
        confidence=_coerce_float(raw.get("confidence")),
        probabilities=_parse_prob_map(raw.get("probabilities")),
        probability=_coerce_float(raw.get("probability")),
        score=_coerce_float(raw.get("score")),
    )


class SystemOneClient:
    """Thin `/v1/systemone` HTTP client supporting multi-question `choice`, `noul`, and `score`."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_DECISION_MODEL,
        timeout_seconds: float = DEFAULT_DECISION_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Decision API key must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("Decision timeout must be positive")
        self.endpoint_url = build_systemone_url(base_url)
        self.api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_DECISION_MODEL
        self.timeout_seconds = float(timeout_seconds)

    def evaluate(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, DecisionAnswer], int]:
        """Call `/v1/systemone` synchronously and return parsed per-question answers and duration_ms."""
        payload = {
            "model": self.model,
            "state": state,
            "questions": questions,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = URLRequest(
            self.endpoint_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "WeChatSupportRouter/1.0",
            },
            method="POST",
        )
        effective_timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        started_at = time.monotonic()
        try:
            with urlopen(req, timeout=effective_timeout) as resp:
                raw_text = resp.read().decode("utf-8")
        except HTTPError as exc:
            raise DecisionClientError(f"http_{exc.code}", status_code=exc.code) from None
        except (TimeoutError, OSError, URLError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(exc).lower():
                raise DecisionClientError("timeout") from None
            raise DecisionClientError(f"transport_{type(exc).__name__}") from None

        duration_ms = round((time.monotonic() - started_at) * 1000)
        try:
            data = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise DecisionClientError("invalid_json") from None
        if not isinstance(data, dict):
            raise DecisionClientError("non_object_response")

        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            # Support gateways wrapping response inside `data` or `results`.
            raw_answers = data.get("data") if isinstance(data.get("data"), dict) else data.get("results")
        if not isinstance(raw_answers, dict):
            raise DecisionClientError("missing_answers")

        answers = {
            qid: _parse_answer(raw_answers.get(qid))
            for qid in questions
        }
        return answers, duration_ms
