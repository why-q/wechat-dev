"""Step 3 orchestrator: Exact Alias -> `/v1/systemone` routing -> Tier 2 RAG -> Clarify / Fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from pathlib import Path
import threading
from typing import Any, Awaitable, Callable

from .db import FlushOutcome, SupportStore
from .decision_client import DecisionClientError, SystemOneClient
from .knowledge import KnowledgeError, Tier2KnowledgeResponder, strip_model_urls
from .presets import PresetCatalog

FALLBACK_TEMPLATE_REPLY = (
    "抱歉，当前自动客服暂无法准确确认您的具体情况。我们已为您登记至馆员待办队列，"
    "工作人员将在图书馆公众号后台核实后为您回复。\n"
    "如需加急人工咨询，也可致电：\n"
    "• 咨询台：0754-8650 3247（服务时间：8:15–11:45、14:30–17:00，节假日及寒暑假除外）\n"
    "• 桑浦山校区总服务台：0754-8650 2958\n"
    "• 东海岸校区服务台：0754-8650 2999"
)

REOPENED_CASE_FOLLOWUP_REPLY = (
    "已收到您的补充追问，已自动关联至原咨询记录并重新提交馆员跟进，请稍候回复。"
)

DEFAULT_CLARIFY_QUESTION = (
    "为了更准确地解答您的问题，方便补充一下具体情况吗（例如涉及哪个校区、具体业务名称或系统报错提示）？"
)

DEFAULT_PRESET_MIN_CONFIDENCE = 0.65

ClarifyGeneratorFn = Callable[[str, str], Awaitable[str] | str]
DeliverReplyFn = Callable[..., Awaitable[int]]

_LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True, slots=True)
class TriagePlan:
    """Computed routing outcome before post-commit database mutations and outbound send."""

    stage_path: str
    clarified_already: bool
    combined_question: str
    reply_text: str = ""
    trigger_kind: str = "triage_reply"
    decision_route: str = "skipped"
    decision_preset_id: str = ""
    decision_confidence: float | None = None
    decision_duration_ms: int | None = None
    preset_id: str = ""
    source_url: str = ""
    section_title: str = ""
    consume_clarification: bool = False
    set_clarification_prompt: str = ""
    create_staff_todo: bool = False
    staff_summary: str = ""


class SupportResponder:
    """Routes committed/ready batches through Tier 1 aliases, `/v1/systemone`, and Tier 2 `0.` RAG."""

    def __init__(
        self,
        *,
        store: SupportStore,
        preset_catalog: PresetCatalog,
        decision_client: SystemOneClient | None = None,
        knowledge_responder: Tier2KnowledgeResponder | None = None,
        clarify_generator: ClarifyGeneratorFn | None = None,
        preset_min_confidence: float = DEFAULT_PRESET_MIN_CONFIDENCE,
        clarify_ttl_seconds: float = 1800.0,
        corpus_dir: Path | None = None,
        dynamic_presets_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.preset_catalog = preset_catalog
        self.decision_client = decision_client
        self.knowledge_responder = knowledge_responder
        self.clarify_generator = clarify_generator
        self.preset_min_confidence = float(preset_min_confidence)
        self.clarify_ttl_seconds = float(clarify_ttl_seconds)
        self.corpus_dir = Path(corpus_dir) if corpus_dir is not None else None
        self.dynamic_presets_dir = Path(dynamic_presets_dir) if dynamic_presets_dir is not None else None
        self._preset_lock = threading.RLock()

    def reload_presets(self) -> int:
        """Thread-safely reload the runtime PresetCatalog from static + dynamic corpus directories."""
        with self._preset_lock:
            if self.corpus_dir is None and self.dynamic_presets_dir is None:
                return len(self.preset_catalog.presets)
            new_catalog = PresetCatalog.from_corpus_dirs(self.corpus_dir, self.dynamic_presets_dir)
            self.preset_catalog = new_catalog
            return len(new_catalog.presets)

    @staticmethod
    def _fallback_plan(
        *,
        stage_path: str,
        trigger_kind: str,
        clarified_already: bool,
        combined_question: str,
        decision_route: str,
        decision_preset_id: str,
        decision_confidence: float | None,
        decision_duration_ms: int | None,
        create_staff_todo: bool = True,
    ) -> TriagePlan:
        return TriagePlan(
            stage_path=stage_path,
            clarified_already=clarified_already,
            combined_question=combined_question,
            reply_text=FALLBACK_TEMPLATE_REPLY,
            trigger_kind=trigger_kind,
            decision_route=decision_route,
            decision_preset_id=decision_preset_id,
            decision_confidence=decision_confidence,
            decision_duration_ms=decision_duration_ms,
            consume_clarification=clarified_already,
            create_staff_todo=create_staff_todo,
            staff_summary=combined_question[:120] if create_staff_todo else "",
        )

    async def triage(self, outcome: FlushOutcome) -> TriagePlan:
        """Determine the exact answer/clarify/fallback plan without mutating durable state."""
        if outcome.route_stage == "staff_case_appended":
            return TriagePlan(
                stage_path="staff_case_appended",
                clarified_already=False,
                combined_question=outcome.batch.merged_text or "",
                decision_route="skipped",
            )

        merged_text = (outcome.batch.merged_text or "").strip()
        account_id = outcome.batch.account_id
        reader_id = outcome.batch.reader_id
        active_clarify = await asyncio.to_thread(
            self.store.get_active_clarification, account_id, reader_id
        )
        clarified_already = active_clarify is not None
        combined_question = (
            f"{active_clarify.original_question}\n补充说明：{merged_text}"
            if active_clarify is not None
            else merged_text
        )

        with self._preset_lock:
            preset_catalog = self.preset_catalog

        # Tier 1a: Exact symmetric alias match against `1.` presets.
        exact_hit = preset_catalog.match_exact(merged_text)
        if exact_hit is not None:
            return TriagePlan(
                stage_path="exact_alias",
                clarified_already=clarified_already,
                combined_question=combined_question,
                reply_text=exact_hit.body,
                trigger_kind="preset_exact",
                decision_route="skipped",
                preset_id=exact_hit.preset_id,
                consume_clarification=clarified_already,
            )

        # Tier 1b: `/v1/systemone` multi-question `Choice` decision (default 3.0s bounded).
        decision_route = "unconfigured"
        decision_preset_id = ""
        decision_confidence: float | None = None
        decision_duration_ms: int | None = None

        if self.decision_client is not None:
            try:
                answers, duration_ms = await asyncio.to_thread(
                    self._call_decision_model,
                    preset_catalog=preset_catalog,
                    combined_question=combined_question,
                    clarified_already=clarified_already,
                    original_question=active_clarify.original_question if active_clarify else "",
                    clarify_prompt=active_clarify.clarify_prompt if active_clarify else "",
                    reader_reply=merged_text if active_clarify else "",
                )
                decision_duration_ms = duration_ms
                route_ans = answers.get("route")
                preset_ans = answers.get("preset_id")
                decision_route = (route_ans.choice if route_ans and route_ans.choice else "unknown").strip()
                decision_preset_id = (preset_ans.choice if preset_ans and preset_ans.choice else "none").strip()
                decision_confidence = preset_ans.confidence if preset_ans else None
            except DecisionClientError as exc:
                decision_route = "timeout" if exc.reason_code == "timeout" else "error"
                _LOGGER.warning(
                    "Decision router failed, degrading to Tier 2: batch_id=%d reason=%s",
                    outcome.batch.batch_id,
                    exc.reason_code,
                )
            except Exception as exc:
                decision_route = "error"
                _LOGGER.warning(
                    "Decision router unexpected failure, degrading to Tier 2: batch_id=%d reason=%s",
                    outcome.batch.batch_id,
                    type(exc).__name__,
                )

        if decision_route == "staff_action":
            return self._fallback_plan(
                stage_path="decision_staff",
                trigger_kind="fallback_staff",
                clarified_already=clarified_already,
                combined_question=combined_question,
                decision_route=decision_route,
                decision_preset_id=decision_preset_id,
                decision_confidence=decision_confidence,
                decision_duration_ms=decision_duration_ms,
                create_staff_todo=True,
            )

        if decision_route == "unknown":
            return self._fallback_plan(
                stage_path="decision_unknown_fallback",
                trigger_kind="fallback_unknown",
                clarified_already=clarified_already,
                combined_question=combined_question,
                decision_route=decision_route,
                decision_preset_id=decision_preset_id,
                decision_confidence=decision_confidence,
                decision_duration_ms=decision_duration_ms,
                create_staff_todo=False,
            )

        if decision_route == "clarify":
            if clarified_already:
                return self._fallback_plan(
                    stage_path="clarify_limit_fallback",
                    trigger_kind="fallback_clarify_limit",
                    clarified_already=True,
                    combined_question=combined_question,
                    decision_route=decision_route,
                    decision_preset_id=decision_preset_id,
                    decision_confidence=decision_confidence,
                    decision_duration_ms=decision_duration_ms,
                    create_staff_todo=True,
                )
            if reader_id:
                recent_match = await asyncio.to_thread(
                    self.store.find_recent_closed_matching_case,
                    account_id=account_id,
                    reader_id=reader_id,
                    merged_text=merged_text,
                    reopen_window_seconds=1800.0,
                )
                if recent_match is not None:
                    return self._fallback_plan(
                        stage_path="reopened_closed_case_followup",
                        trigger_kind="staff_case_reopened",
                        clarified_already=False,
                        combined_question=combined_question,
                        decision_route=decision_route,
                        decision_preset_id=decision_preset_id,
                        decision_confidence=decision_confidence,
                        decision_duration_ms=decision_duration_ms,
                        create_staff_todo=True,
                    )
            clarify_q = await self._generate_clarify_question(combined_question)
            return TriagePlan(
                stage_path="decision_clarify",
                clarified_already=False,
                combined_question=combined_question,
                reply_text=clarify_q,
                trigger_kind="clarify",
                decision_route=decision_route,
                decision_preset_id=decision_preset_id,
                decision_confidence=decision_confidence,
                decision_duration_ms=decision_duration_ms,
                set_clarification_prompt=clarify_q,
            )

        if decision_route == "public_qa" and decision_preset_id and decision_preset_id != "none":
            chosen_preset = preset_catalog.get_by_id(decision_preset_id)
            confidence_ok = (
                decision_confidence is None or decision_confidence >= self.preset_min_confidence
            )
            if chosen_preset is not None and confidence_ok:
                return TriagePlan(
                    stage_path="decision_preset",
                    clarified_already=clarified_already,
                    combined_question=combined_question,
                    reply_text=chosen_preset.body,
                    trigger_kind="preset_decision",
                    decision_route=decision_route,
                    decision_preset_id=decision_preset_id,
                    decision_confidence=decision_confidence,
                    decision_duration_ms=decision_duration_ms,
                    preset_id=chosen_preset.preset_id,
                    consume_clarification=clarified_already,
                )

        # Tier 2: Public `0.` corpus RAG (also catches Tier 1 timeout/error/none).
        return await self._run_tier2(
            batch_id=outcome.batch.batch_id,
            combined_question=combined_question,
            clarification_context=(
                f"原问题：{active_clarify.original_question}\n追问：{active_clarify.clarify_prompt}"
                if active_clarify is not None
                else ""
            ),
            clarified_already=clarified_already,
            decision_route=decision_route,
            decision_preset_id=decision_preset_id,
            decision_confidence=decision_confidence,
            decision_duration_ms=decision_duration_ms,
        )

    def _call_decision_model(
        self,
        *,
        combined_question: str,
        clarified_already: bool,
        original_question: str,
        clarify_prompt: str,
        reader_reply: str,
        preset_catalog: PresetCatalog | None = None,
    ):
        assert self.decision_client is not None
        active_catalog = preset_catalog if preset_catalog is not None else self.preset_catalog
        preset_list = [
            {
                "id": p.preset_id,
                "title": p.title,
                "applicability": p.applicability,
                "answer_body": p.body,
            }
            for p in active_catalog.presets
        ]
        state = {
            "question": combined_question,
            "clarification_history": (
                {
                    "already_clarified": True,
                    "original_question": original_question,
                    "clarify_prompt": clarify_prompt,
                    "reader_reply": reader_reply,
                }
                if clarified_already
                else None
            ),
            "presets": preset_list,
        }
        route_criteria = {
            "public_qa": "公开信息咨询、借还续借预约/开闭馆规则查询或通用功能排障；只要业务意图明确（未说明校区或图书类别时默认按常规普通规则理解），直接匹配预设或进 Tier 2 检索，不要轻易反问澄清。",
            "clarify": "仅当留言过于简略或存在根本歧义（例如只发「预约」「办证」「那个用不了」），不追问就无法判断对应哪种馆内业务时才选。"
            if not clarified_already
            else "已澄清过一次仍缺少核心业务意图（调用方将按单次澄清上限转 Fallback 待办）。",
            "staff_action": "明确需要馆员后台核查个人账号/借阅/扣费记录、退费免罚、处置现场占座噪音投诉或人工干预。",
            "unknown": "无明确语义、闲聊、或完全超出图书馆服务与资源范围。",
        }
        preset_criteria = {
            p.preset_id: f"当读者询问的问题（含续借天数/次数/步骤、开闭馆时间或预约流程）被固定答案「{p.title}」正文与适用条件({p.applicability})覆盖且无报错/异常诉求时选此项。"
            for p in active_catalog.presets
        }
        preset_criteria["none"] = "无适用固定答案（含报错排障、规则细节、适用条件外问题或无需固定答案）。"

        questions = {
            "route": {
                "type": "choice",
                "instructions": "判断读者留言的下一步客服路由。",
                "criteria": route_criteria,
            },
            "preset_id": {
                "type": "choice",
                "instructions": "判断是否有一条 `1.` 固定答案可直接完整回答读者诉求；若不完全适用务必选 none。",
                "criteria": preset_criteria,
            },
        }
        return self.decision_client.evaluate(state=state, questions=questions)

    async def _generate_clarify_question(self, combined_question: str) -> str:
        if self.clarify_generator is None:
            return DEFAULT_CLARIFY_QUESTION
        try:
            raw = self.clarify_generator(
                combined_question,
                "读者提出了模糊的图书馆咨询，请用一句简短、礼貌的中文提问引导读者补充关键细节（如校区、业务类型或报错提示），不超过45字，禁止输出链接。",
            )
            if inspect.isawaitable(raw):
                raw = await raw
            cleaned = strip_model_urls(str(raw or ""))
            return cleaned[:120] if cleaned else DEFAULT_CLARIFY_QUESTION
        except Exception as exc:
            _LOGGER.warning("Clarify question generation failed: reason=%s", type(exc).__name__)
            return DEFAULT_CLARIFY_QUESTION

    async def _run_tier2(
        self,
        *,
        batch_id: int,
        combined_question: str,
        clarification_context: str,
        clarified_already: bool,
        decision_route: str,
        decision_preset_id: str,
        decision_confidence: float | None,
        decision_duration_ms: int | None,
    ) -> TriagePlan:
        fallback_kwargs: dict[str, Any] = {
            "clarified_already": clarified_already,
            "combined_question": combined_question,
            "decision_route": decision_route,
            "decision_preset_id": decision_preset_id,
            "decision_confidence": decision_confidence,
            "decision_duration_ms": decision_duration_ms,
        }
        if self.knowledge_responder is None:
            return self._fallback_plan(
                stage_path="tier2_unavailable_fallback",
                trigger_kind="fallback_tier2_unavailable",
                **fallback_kwargs,
            )
        try:
            result = await asyncio.to_thread(
                self.knowledge_responder.query,
                combined_question,
                clarification_context=clarification_context,
                allow_clarify=not clarified_already,
            )
        except Exception as exc:
            if isinstance(exc, KnowledgeError):
                _LOGGER.warning("Tier 2 RAG failed: batch_id=%d code=%s", batch_id, exc.code)
            else:
                _LOGGER.warning("Tier 2 RAG unexpected error: batch_id=%d reason=%s", batch_id, type(exc).__name__)
            return self._fallback_plan(
                stage_path="tier2_error_fallback",
                trigger_kind="fallback_tier2_error",
                **fallback_kwargs,
            )

        if result.outcome == "answered":
            stage = "tier2_long" if result.is_long_formatted else "tier2_short"
            return TriagePlan(
                stage_path=stage,
                clarified_already=clarified_already,
                combined_question=combined_question,
                reply_text=result.reply_text,
                trigger_kind=stage,
                decision_route=decision_route,
                decision_preset_id=decision_preset_id,
                decision_confidence=decision_confidence,
                decision_duration_ms=decision_duration_ms,
                source_url=result.source_url,
                section_title=result.section_title,
                consume_clarification=clarified_already,
            )

        if result.outcome == "need_clarify" and not clarified_already and result.clarify_question:
            return TriagePlan(
                stage_path="tier2_clarify",
                clarified_already=False,
                combined_question=combined_question,
                reply_text=result.clarify_question,
                trigger_kind="clarify",
                decision_route=decision_route,
                decision_preset_id=decision_preset_id,
                decision_confidence=decision_confidence,
                decision_duration_ms=decision_duration_ms,
                set_clarification_prompt=result.clarify_question,
            )

        if result.outcome == "staff_action":
            return self._fallback_plan(
                stage_path="tier2_staff",
                trigger_kind="fallback_staff",
                **fallback_kwargs,
            )

        return self._fallback_plan(
            stage_path="tier2_fallback",
            trigger_kind="fallback_unsupported",
            **fallback_kwargs,
        )

    async def apply_committed_plan(
        self,
        outcome: FlushOutcome,
        plan: TriagePlan | None,
        *,
        deliver_reply_fn: DeliverReplyFn,
    ) -> None:
        """Apply durable DB side effects (clarification/staff todo/triage audit) and deliver reply."""
        if plan is None:
            return
        account_id = outcome.batch.account_id
        reader_id = outcome.batch.reader_id
        batch_id = outcome.batch.batch_id

        if plan.consume_clarification and reader_id:
            await asyncio.to_thread(self.store.consume_active_clarification, account_id, reader_id)

        if plan.set_clarification_prompt and reader_id:
            await asyncio.to_thread(
                self.store.set_active_clarification,
                account_id=account_id,
                reader_id=reader_id,
                original_batch_id=batch_id,
                original_question=plan.combined_question,
                clarify_prompt=plan.set_clarification_prompt,
                ttl_seconds=self.clarify_ttl_seconds,
            )

        created_case_id: int | None = outcome.staff_case_id
        reopened_existing = False
        if plan.create_staff_todo and reader_id:
            source_event_seq = outcome.event_seqs[-1] if outcome.event_seqs else None
            # Prefer reopening a recently closed matching case to preserve continuous conversation context.
            reopened = await asyncio.to_thread(
                self.store.reopen_recent_closed_staff_case_if_matching,
                account_id=account_id,
                reader_id=reader_id,
                merged_text=plan.combined_question,
                source_batch_id=batch_id,
                source_event_seq=source_event_seq,
            )
            if reopened is not None:
                created_case_id = reopened.case_id
                reopened_existing = True
            else:
                # Always create staff to-do BEFORE outbound send so delivery failure never loses follow-up.
                case = await asyncio.to_thread(
                    self.store.create_staff_case,
                    account_id=account_id,
                    reader_id=reader_id,
                    summary=plan.staff_summary or plan.combined_question[:120],
                    latest_message=plan.combined_question,
                    source_batch_id=batch_id,
                    source_event_seq=source_event_seq,
                    case_kind="staff_todo",
                )
                created_case_id = case.case_id

        reply_to_send = (
            REOPENED_CASE_FOLLOWUP_REPLY
            if reopened_existing and plan.reply_text == FALLBACK_TEMPLATE_REPLY
            else plan.reply_text
        )
        stage_to_record = (
            "reopened_closed_case_followup"
            if reopened_existing and plan.stage_path != "reopened_closed_case_followup"
            else plan.stage_path
        )

        await asyncio.to_thread(
            self.store.record_triage_run,
            batch_id=batch_id,
            account_id=account_id,
            reader_id=reader_id,
            generation=outcome.batch.generation,
            clarified_already=plan.clarified_already,
            stage_path=stage_to_record,
            decision_route=plan.decision_route,
            decision_preset_id=plan.decision_preset_id,
            decision_confidence=plan.decision_confidence,
            decision_duration_ms=plan.decision_duration_ms,
            preset_id=plan.preset_id,
            source_url=plan.source_url,
            section_title=plan.section_title,
            reply_preview=reply_to_send,
            staff_case_id=created_case_id,
        )

        if reply_to_send and reader_id:
            await deliver_reply_fn(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind="staff_case_reopened" if reopened_existing else plan.trigger_kind,
                trigger_ref_id=created_case_id if created_case_id is not None else batch_id,
                content=reply_to_send,
            )
