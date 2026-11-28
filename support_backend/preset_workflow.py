"""AI preset draft generation, deterministic guardrails, independent dual-review gate, and diff helper."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
import re
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from .presets import PresetCatalog, normalize_alias_text

_PRESET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")
_URL_RE = re.compile(r"https?://[^\s<>()\]\"']+")
_LIB_PHONE_AREA_RE = re.compile(r"(?<!\d)0754[-\s]*\d{4}[-\s]*\d{4}(?!\d)")
_LIB_PHONE_LOCAL_RE = re.compile(r"(?<!\d)8650[-\s]?\d{4}(?!\d)")
_MOBILE_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{8,18}(?!\d)")
_ONE_OFF_PHRASES: tuple[str, ...] = (
    "已帮您",
    "已为您",
    "刚为您",
    "刚才帮您",
    "您的学号",
    "您的借阅证号",
    "已手动为您",
    "已帮同学",
    "特例为您",
)
_CODE_FENCE_RE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*\Z", re.DOTALL | re.IGNORECASE)

FALLBACK_GENERATION_NOTE = "AI 转换暂不可用，已生成基础草稿，请人工编辑并脱敏后再提交审核。"
DETERMINISTIC_AUDIT_SUGGESTION = "请先修正上述格式、冲突或隐私/个案问题后再提交发布。"
AI_UNAVAILABLE_REASON = "AI 审核服务暂不可用，为防止误发布未脱敏内容已拦截。"
AI_UNAVAILABLE_SUGGESTION = "请检查 RAG_LLM_* 配置或稍后重试。"


@dataclass(frozen=True, slots=True)
class PresetDraftProposal:
    """AI-generated or fallback proposal for creating or merging a `1.` preset."""

    mode: str  # "create" or "merge"
    target_preset_id: str
    preset_id: str
    title: str
    aliases: tuple[str, ...]
    applicability: str
    body: str
    note: str


@dataclass(frozen=True, slots=True)
class PresetAuditResult:
    """Outcome of deterministic + independent AI review gate before publishing."""

    passed: bool
    reasons: tuple[str, ...]
    suggestion: str
    suggested_mode: str = ""
    suggested_target_id: str = ""
    checker: str = "deterministic+ai"


def _is_valid_http_base(url: str) -> bool:
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        return False
    try:
        parsed = urlsplit(cleaned)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )


def _extract_json_dict(raw_content: str) -> dict[str, Any]:
    text = raw_content.strip()
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match is not None:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as inner_exc:
                raise RuntimeError(f"LLM 返回非合法 JSON：{inner_exc}") from inner_exc
        else:
            raise RuntimeError(f"LLM 返回非合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 返回的 JSON 顶层必须是对象")
    return parsed


class OpenAICompatiblePresetLLM:
    """Minimal stdlib OpenAI-compatible JSON completion client using `RAG_LLM_*` env vars."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 25.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        source_env = os.environ if env is None else env
        self.api_base = (api_base if api_base is not None else source_env.get("RAG_LLM_API_BASE", "")).strip()
        self.api_key = (api_key if api_key is not None else source_env.get("RAG_LLM_API_KEY", "")).strip()
        self.model = (model if model is not None else source_env.get("RAG_LLM_MODEL", "")).strip()
        self.timeout_seconds = float(timeout_seconds)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_base and self.api_key and self.model and _is_valid_http_base(self.api_base))

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("RAG_LLM_* 环境变量未完整配置或 API Base 格式无效")

        endpoint = f"{self.api_base.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                detail = str(exc.reason)
            raise RuntimeError(f"LLM HTTP 错误 {exc.code}: {detail}") from exc
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"LLM 请求失败：{exc}") from exc

        try:
            response_json = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM 响应非合法 JSON：{exc}") from exc

        choices = response_json.get("choices") if isinstance(response_json, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM 响应缺少 choices 字段")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM 响应缺少 message.content 内容")
        return _extract_json_dict(content)


def _strip_allowed_public_tokens(text: str) -> str:
    cleaned = _URL_RE.sub(" ", text)
    cleaned = _LIB_PHONE_AREA_RE.sub(" ", cleaned)
    cleaned = _LIB_PHONE_LOCAL_RE.sub(" ", cleaned)
    return cleaned


def _clean_aliases(aliases: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in aliases:
        item = str(raw).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def deterministic_preset_guardrails(
    *,
    mode: str,
    target_preset_id: str,
    preset_id: str,
    title: str,
    aliases: Sequence[str],
    applicability: str,
    body: str,
    catalog: PresetCatalog,
) -> list[str]:
    """Run deterministic format, collision, and PII/one-off leakage guardrails."""
    reasons: list[str] = []
    clean_mode = str(mode).strip()
    clean_target_id = str(target_preset_id).strip()
    clean_id = str(preset_id).strip()
    clean_title = str(title).strip()
    clean_applicability = str(applicability).strip()
    clean_body = str(body).strip()
    cleaned_aliases = _clean_aliases(aliases)

    if clean_mode not in {"create", "merge"}:
        reasons.append(f"发布模式无效（{clean_mode or '空'}），仅支持 create 或 merge。")

    if not _PRESET_ID_RE.match(clean_id):
        reasons.append("preset_id 格式不合法，须为 3-64 位字母、数字、下划线或短横线。")

    if len(clean_title) < 2:
        reasons.append("标题不能为空且至少包含 2 个字符。")

    if len(clean_applicability) < 8:
        reasons.append("适用条件（applicability）过短，须至少 8 个字符以明确边界。")

    if len(clean_body) < 8:
        reasons.append("回复正文（body）过短，须至少 8 个字符。")

    if clean_mode == "merge":
        if not clean_target_id or catalog.get_by_id(clean_target_id) is None:
            reasons.append(f"并入目标模板不存在：{clean_target_id or '空'}。")
        elif clean_id != clean_target_id:
            reasons.append(f"并入模式下 preset_id（{clean_id}）必须与 target_preset_id（{clean_target_id}）一致。")
    elif clean_mode == "create":
        if clean_id and catalog.get_by_id(clean_id) is not None:
            reasons.append(f"preset_id「{clean_id}」已存在，请改用 merge 模式并入或更换新 ID。")

    valid_alias_count = 0
    seen_norm_aliases: set[str] = set()
    for alias in cleaned_aliases:
        norm = normalize_alias_text(alias)
        if not norm:
            reasons.append(f"别名「{alias}」去除标点后为空，请删除或修改。")
            continue
        if len(norm) <= 1:
            reasons.append(f"别名「{alias}」过短（单字易误触发），请使用更具体的短语。")
            continue
        valid_alias_count += 1
        if norm in seen_norm_aliases:
            continue
        seen_norm_aliases.add(norm)
        existing = catalog.match_exact(alias)
        if existing is not None:
            if not (clean_mode == "merge" and existing.preset_id == clean_target_id):
                reasons.append(
                    f"别名「{alias}」与已有模板「{existing.title}」（{existing.preset_id}）冲突。"
                )

    if valid_alias_count == 0:
        reasons.append("至少需要 1 个有效触发别名（aliases）。")

    all_text = "\n".join([clean_title, *cleaned_aliases, clean_applicability, clean_body])
    stripped_for_pii = _strip_allowed_public_tokens(all_text)

    if _MOBILE_PHONE_RE.search(stripped_for_pii):
        reasons.append("检测到疑似个人手机号，请脱敏删除后再提交。")

    if _EMAIL_RE.search(stripped_for_pii):
        reasons.append("检测到疑似电子邮箱地址，请脱敏删除后再提交。")

    without_mobile = _MOBILE_PHONE_RE.sub(" ", stripped_for_pii)
    if _LONG_DIGITS_RE.search(without_mobile):
        reasons.append("检测到疑似学号、借阅证号、图书条码或证件号（8-18 位连续数字），请脱敏删除。")

    matched_phrases = [phrase for phrase in _ONE_OFF_PHRASES if phrase in all_text]
    if matched_phrases:
        reasons.append(
            "检测到一次性人工个案处理措辞（" + "、".join(matched_phrases) + "），请改写为通用规则说明。"
        )

    return reasons


def _build_catalog_digest(catalog: PresetCatalog) -> list[dict[str, Any]]:
    return [
        {
            "id": p.preset_id,
            "title": p.title,
            "aliases": list(p.aliases),
            "applicability": p.applicability,
            "body": p.body,
        }
        for p in catalog.presets
    ]


def _make_fallback_preset_id(question: str, staff_reply: str, catalog: PresetCatalog) -> str:
    digest = hashlib.sha1(f"{question}\n{staff_reply}".encode("utf-8")).hexdigest()[:8]
    candidate = f"preset_draft_{digest}"
    suffix = 1
    while catalog.get_by_id(candidate) is not None:
        candidate = f"preset_draft_{digest}_{suffix}"
        suffix += 1
    return candidate


def _build_fallback_proposal(question: str, staff_reply: str, catalog: PresetCatalog) -> PresetDraftProposal:
    cleaned_q = " ".join(question.strip().split())
    trimmed_title = re.sub(r"[？?！!。.,，、~～]+$", "", cleaned_q)[:24].strip()
    if len(trimmed_title) < 2:
        trimmed_title = "常见业务咨询"
    alias_norm = normalize_alias_text(trimmed_title)
    aliases = (trimmed_title,) if len(alias_norm) > 1 else ("常见业务咨询",)
    cleaned_body = staff_reply.strip() or cleaned_q or "请补充标准回复正文。"
    return PresetDraftProposal(
        mode="create",
        target_preset_id="",
        preset_id=_make_fallback_preset_id(question, staff_reply, catalog),
        title=trimmed_title,
        aliases=aliases,
        applicability=f"仅适用于读者咨询「{trimmed_title}」相关的常规公开规则与办理流程，不适用于个人账号核查或异常报错排障。",
        body=cleaned_body,
        note=FALLBACK_GENERATION_NOTE,
    )


def _normalize_generated_preset_id(raw_id: str, question: str, staff_reply: str, catalog: PresetCatalog) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_id.strip().lower()).strip("_")
    if len(cleaned) < 3 or not _PRESET_ID_RE.match(cleaned):
        return _make_fallback_preset_id(question, staff_reply, catalog)
    cleaned = cleaned[:64]
    if catalog.get_by_id(cleaned) is None:
        return cleaned
    return _make_fallback_preset_id(question, staff_reply, catalog)


def generate_preset_proposal(
    *,
    question: str,
    staff_reply: str,
    catalog: PresetCatalog,
    llm_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> PresetDraftProposal:
    """Use LLM (or safe fallback) to generalize a staff reply into a create/merge `1.` preset proposal."""
    caller = llm_fn
    if caller is None:
        client = OpenAICompatiblePresetLLM()
        if not client.is_configured:
            return _build_fallback_proposal(question, staff_reply, catalog)
        caller = client.complete_json

    system_prompt = (
        "你是汕头大学图书馆客服知识库整理专家，负责将馆员对读者的具体答复转换为可复用的 `1.` 标准固定答案模板。\n"
        "严格要求：\n"
        "1. 必须彻底去除所有个人隐私信息与个案细节（姓名、学号、借阅证号、手机号、邮箱、具体图书条码、一次性人工处理措辞如「已帮您」「刚为您」等），改写为面向全体读者的通用规则与操作指南。\n"
        "2. 仔细对比现有全部 `1.` 模板：如果本次问答与某条已有模板属于同一服务类别/同一业务主题，必须选择 mode=\"merge\"，将 target_preset_id 和 preset_id 设为该已有模板的 id，并在充分保留原模板核心规则的前提下融合改写出完整连贯的新 body、补充/精炼 aliases 与 applicability（绝对不要简单机械拼接或重复段落）。\n"
        "3. 只有当现有模板中不存在同类业务主题时，才选择 mode=\"create\"，将 target_preset_id 设为 \"\"，并生成以 preset_ 开头的英文下划线 preset_id。\n"
        "4. aliases 必须是精准的整句常见问法短语（至少 2 个字符，不可用单字，不可与其它模板冲突，避免误触发报错/投诉类提问）。\n"
        "5. applicability 必须清晰界定适用场景与排除边界（至少 8 个字符）。\n"
        "6. 仅输出严格 JSON 对象，包含字段：mode, target_preset_id, preset_id, title, aliases, applicability, body, note。"
    )
    user_payload = {
        "reader_question": question.strip(),
        "staff_reply": staff_reply.strip(),
        "existing_presets": _build_catalog_digest(catalog),
    }
    user_prompt = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        raw = caller(system_prompt, user_prompt)
    except Exception:
        return _build_fallback_proposal(question, staff_reply, catalog)

    if not isinstance(raw, dict):
        return _build_fallback_proposal(question, staff_reply, catalog)

    raw_mode = str(raw.get("mode") or "create").strip().lower()
    raw_target_id = str(raw.get("target_preset_id") or "").strip()
    raw_preset_id = str(raw.get("preset_id") or "").strip()

    if raw_mode == "merge" and raw_target_id and catalog.get_by_id(raw_target_id) is not None:
        mode = "merge"
        target_preset_id = raw_target_id
        preset_id = raw_target_id
    else:
        mode = "create"
        target_preset_id = ""
        preset_id = _normalize_generated_preset_id(raw_preset_id, question, staff_reply, catalog)

    title = str(raw.get("title") or "").strip()
    if not title:
        existing = catalog.get_by_id(target_preset_id) if mode == "merge" else None
        title = existing.title if existing is not None else "常见业务咨询"

    raw_aliases = raw.get("aliases")
    if isinstance(raw_aliases, (list, tuple)):
        aliases = tuple(_clean_aliases([str(item) for item in raw_aliases]))
    elif isinstance(raw_aliases, str) and raw_aliases.strip():
        aliases = (raw_aliases.strip(),)
    else:
        aliases = (title,)

    applicability = str(raw.get("applicability") or "").strip()
    body = str(raw.get("body") or "").strip() or staff_reply.strip()
    note = str(raw.get("note") or "").strip()

    return PresetDraftProposal(
        mode=mode,
        target_preset_id=target_preset_id,
        preset_id=preset_id,
        title=title,
        aliases=aliases,
        applicability=applicability,
        body=body,
        note=note,
    )


def audit_preset_for_publish(
    *,
    mode: str,
    target_preset_id: str,
    preset_id: str,
    title: str,
    aliases: Sequence[str],
    applicability: str,
    body: str,
    catalog: PresetCatalog,
    llm_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> PresetAuditResult:
    """Two-stage publish gate: deterministic guardrails + fail-closed independent AI review."""
    deterministic_reasons = deterministic_preset_guardrails(
        mode=mode,
        target_preset_id=target_preset_id,
        preset_id=preset_id,
        title=title,
        aliases=aliases,
        applicability=applicability,
        body=body,
        catalog=catalog,
    )
    if deterministic_reasons:
        return PresetAuditResult(
            passed=False,
            reasons=tuple(deterministic_reasons),
            suggestion=DETERMINISTIC_AUDIT_SUGGESTION,
            checker="deterministic",
        )

    caller = llm_fn
    if caller is None:
        client = OpenAICompatiblePresetLLM()
        if not client.is_configured:
            return PresetAuditResult(
                passed=False,
                reasons=(AI_UNAVAILABLE_REASON,),
                suggestion=AI_UNAVAILABLE_SUGGESTION,
                checker="ai_unavailable",
            )
        caller = client.complete_json

    clean_mode = str(mode).strip()
    clean_target_id = str(target_preset_id).strip()
    existing_target = catalog.get_by_id(clean_target_id) if clean_mode == "merge" else None

    system_prompt = (
        "你是汕头大学图书馆客服知识库独立终审员，对即将发布上线的 `1.` 固定答案模板执行严格上线前安全与质量审查。\n"
        "必须逐项核查以下 4 点，只要存在任何问题就必须判定 passed=false：\n"
        "1. 隐私与个案残留：检查 title、aliases、applicability、body 是否仍包含任何个人姓名、学号、借阅证号、联系方式、具体册次/条码、一次性特例承诺、或仅供馆员内部查看的备注。\n"
        "2. 业务范畴重复：若 mode=\"create\"，但候选内容与现有 `1.` 模板中的某一条属于同一服务类别或主题，必须驳回（passed=false），并设置 suggested_mode=\"merge\" 与 suggested_target_id=\"<对应已有模板的id>\"，说明应并入该已有模板而非新建重复模板。\n"
        "3. 并入质量：若 mode=\"merge\"，对比旧版 target 模板与新版内容，核查是否干净融合新旧知识、没有丢失原有关键规则，也没有生硬机械拼接或重复段落。\n"
        "4. 触发安全：检查 aliases 与 applicability 是否精准，不会劫持无关投诉、报错排障或需要人工核查的提问。\n"
        "仅输出严格 JSON 对象："
        '{"passed": true/false, "reasons": ["具体驳回原因"], "suggestion": "修改建议", "suggested_mode": "", "suggested_target_id": ""}。'
    )
    user_payload: dict[str, Any] = {
        "candidate": {
            "mode": clean_mode,
            "target_preset_id": clean_target_id,
            "preset_id": str(preset_id).strip(),
            "title": str(title).strip(),
            "aliases": _clean_aliases(aliases),
            "applicability": str(applicability).strip(),
            "body": str(body).strip(),
        },
        "existing_presets": _build_catalog_digest(catalog),
    }
    if existing_target is not None:
        user_payload["existing_target_preset"] = {
            "id": existing_target.preset_id,
            "title": existing_target.title,
            "aliases": list(existing_target.aliases),
            "applicability": existing_target.applicability,
            "body": existing_target.body,
        }
    user_prompt = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        raw = caller(system_prompt, user_prompt)
    except Exception:
        return PresetAuditResult(
            passed=False,
            reasons=(AI_UNAVAILABLE_REASON,),
            suggestion=AI_UNAVAILABLE_SUGGESTION,
            checker="ai_unavailable",
        )

    if not isinstance(raw, dict):
        return PresetAuditResult(
            passed=False,
            reasons=(AI_UNAVAILABLE_REASON,),
            suggestion=AI_UNAVAILABLE_SUGGESTION,
            checker="ai_unavailable",
        )

    passed = raw.get("passed") is True
    raw_reasons = raw.get("reasons")
    reasons_list: list[str] = []
    if isinstance(raw_reasons, (list, tuple)):
        reasons_list = [str(item).strip() for item in raw_reasons if str(item).strip()]
    elif isinstance(raw_reasons, str) and raw_reasons.strip():
        reasons_list = [raw_reasons.strip()]

    suggestion = str(raw.get("suggestion") or "").strip()
    suggested_mode = str(raw.get("suggested_mode") or "").strip()
    suggested_target_id = str(raw.get("suggested_target_id") or "").strip()

    if not passed and not reasons_list:
        reasons_list = ["AI 审核未通过，请根据修改建议调整模板内容后再提交。"]

    return PresetAuditResult(
        passed=passed,
        reasons=tuple(reasons_list),
        suggestion=suggestion,
        suggested_mode=suggested_mode,
        suggested_target_id=suggested_target_id,
        checker="deterministic+ai",
    )


def compute_preset_diff(old_body: str, new_body: str) -> list[dict[str, str]]:
    """Compute a line-level diff list (`same` / `add` / `del`) for HTML merge previews."""
    old_lines = old_body.splitlines()
    new_lines = new_body.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    entries: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in old_lines[i1:i2]:
                entries.append({"kind": "same", "text": line})
        elif tag == "delete":
            for line in old_lines[i1:i2]:
                entries.append({"kind": "del", "text": line})
        elif tag == "insert":
            for line in new_lines[j1:j2]:
                entries.append({"kind": "add", "text": line})
        elif tag == "replace":
            for line in old_lines[i1:i2]:
                entries.append({"kind": "del", "text": line})
            for line in new_lines[j1:j2]:
                entries.append({"kind": "add", "text": line})
    return entries
