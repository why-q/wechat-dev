"""Asynchronous merge / immediate-preemption scheduler, Filter actions, and recovery pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import inspect
import logging
from typing import Any, Awaitable, Callable

from .db import FlushOutcome, IngestResult, SupportStore
from .filters import (
    DEFAULT_GRAY_ENROLL_KEYWORD,
    EMERGENCY_CONTACT_REPLY,
    FEEDBACK_RECEIVED_REPLY,
    GRAY_ENROLL_ADDED_REPLY,
    GRAY_ENROLL_ALREADY_REPLY,
    classify_message,
)
from .shared import safe_errmsg as _safe_errmsg, safe_token

SendTextFn = Callable[[str, str], dict[str, Any]]
BatchFlushedHook = Callable[[FlushOutcome], Awaitable[Any] | Any]
CommitHandlerHook = Callable[[FlushOutcome, Any], Awaitable[None] | None]

_LOGGER = logging.getLogger("uvicorn.error")


def _safe_token(value: str) -> str:
    return safe_token(value, allow_colon=True)


class SupportPipeline:
    """Coordinates Filter, debounce/immediate message dispatch, recovery, and async replies."""

    def __init__(
        self,
        store: SupportStore,
        *,
        send_text_fn: SendTextFn | None = None,
        auto_reply_enabled: bool = False,
        auto_reply_allowlist: Iterable[str] | None = None,
        gray_enroll_keyword: str = DEFAULT_GRAY_ENROLL_KEYWORD,
        triage_fn: BatchFlushedHook | None = None,
        commit_handler: CommitHandlerHook | None = None,
        on_batch_flushed: BatchFlushedHook | None = None,
        purge_interval_seconds: float = 3600.0,
    ) -> None:
        self.store = store
        self.send_text_fn = send_text_fn
        self.auto_reply_enabled = auto_reply_enabled
        self.auto_reply_allowlist: frozenset[str] = (
            frozenset(item.strip() for item in auto_reply_allowlist if isinstance(item, str) and item.strip())
            if auto_reply_allowlist is not None
            else frozenset()
        )
        self.gray_enroll_keyword = gray_enroll_keyword.strip() if isinstance(gray_enroll_keyword, str) else ""
        self.triage_fn = triage_fn
        self.commit_handler = commit_handler
        self.on_batch_flushed = on_batch_flushed
        self.purge_interval_seconds = float(purge_interval_seconds)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._stop_requested = False
        self._scheduler_task: asyncio.Task[None] | None = None
        self._flush_lock: asyncio.Lock | None = None
        self._active_tasks: dict[int, tuple[int, asyncio.Task[None]]] = {}
        self._last_purge_epoch: float = 0.0

    async def purge_expired_now(self) -> int:
        """Purge expired inbound events and auto-close stale `replied` cases."""
        deleted = await asyncio.to_thread(self.store.purge_expired_events)
        auto_closed = await asyncio.to_thread(self.store.auto_close_stale_replied_cases)
        if deleted > 0 or auto_closed > 0:
            _LOGGER.info(
                "Support maintenance finished: deleted_events=%d auto_closed_replied=%d",
                deleted,
                auto_closed,
            )
        return deleted

    async def start(self) -> list[FlushOutcome]:
        """Start background scheduler and recover overdue debounce or interrupted immediate batches."""
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._stop_requested = False

        recovered = await self.flush_due_now()
        try:
            await self.purge_expired_now()
        except Exception as exc:
            _LOGGER.warning("Support startup expiry purge failed: reason=%s", type(exc).__name__)
        self._last_purge_epoch = self.store.now()
        self._scheduler_task = self._loop.create_task(self._scheduler_loop(), name="support-merge-scheduler")

        pending_immediate = await asyncio.to_thread(self.store.recover_immediate_batches)
        for batch in pending_immediate:
            self._spawn_immediate_task(batch.batch_id, batch.generation)

        next_deadline = self.store.next_open_deadline()
        _LOGGER.info(
            "Support pipeline started: merge_enabled=%s max_preempts=%d recovered_open=%d recovered_immediate=%d pending_open_deadline=%s auto_reply=%s gray_enroll=%s",
            str(self.store.merge_enabled).lower(),
            self.store.max_preempts,
            len(recovered),
            len(pending_immediate),
            f"{next_deadline:.3f}" if next_deadline is not None else "-",
            str(self.auto_reply_enabled).lower(),
            str(bool(self.gray_enroll_keyword)).lower(),
        )
        return recovered

    async def stop(self) -> None:
        """Stop timers and cancel in-flight immediate tasks while keeping SQLite batches recoverable."""
        self._stop_requested = True
        if self._wakeup is not None:
            self._wakeup.set()
        if self._scheduler_task is not None:
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

        active = [task for _, task in self._active_tasks.values()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active_tasks.clear()
        self._wakeup = None
        self._loop = None

    def notify_scheduler(self) -> None:
        """Wake the merge scheduler on its running event loop when a deadline may have changed."""
        if self._loop is None or self._wakeup is None or self._loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._wakeup.set()
        else:
            self._loop.call_soon_threadsafe(self._wakeup.set)

    def ingest_sync(self, payload: dict[str, Any], *, client_ip: str = "") -> IngestResult:
        """Synchronously classify and commit one inbound payload to SQLite."""
        msg_type = payload.get("msg_type") if isinstance(payload.get("msg_type"), str) else ""
        content = payload.get("content") if isinstance(payload.get("content"), str) else ""
        event = payload.get("event") if isinstance(payload.get("event"), str) else ""
        decision = classify_message(
            msg_type=msg_type,
            content=content,
            event=event,
            gray_enroll_keyword=self.gray_enroll_keyword,
        )
        result = self.store.ingest_event(payload, decision, client_ip=client_ip)
        _LOGGER.info(
            "Support ingest persisted: seq=%d duplicate=%s category=%s reason=%s batch_id=%s count=%s gen=%s",
            result.seq,
            str(result.duplicate).lower(),
            result.filter_decision.category,
            _safe_token(result.filter_decision.reason) or "-",
            result.batch.batch_id if result.batch is not None else "-",
            result.batch.message_count if result.batch is not None else "-",
            result.batch.generation if result.batch is not None else "-",
        )
        return result

    async def process_ingest_side_effects(self, result: IngestResult, payload: dict[str, Any]) -> None:
        """Execute post-commit actions: contact/feedback replies, pre-flushes, or merge/preempt."""
        for pre in result.pre_flushed:
            await self._dispatch_flushed_outcome(pre, run_triage=True)

        if result.duplicate:
            return

        category = result.filter_decision.category
        account_id = payload.get("to_user") if isinstance(payload.get("to_user"), str) else "default"
        reader_id = payload.get("from_user") if isinstance(payload.get("from_user"), str) else ""

        if category == "emergency" and reader_id:
            await self._deliver_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind="emergency",
                trigger_ref_id=result.staff_case_id,
                content=EMERGENCY_CONTACT_REPLY,
            )
        elif category == "feedback" and reader_id:
            await self._deliver_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind="feedback",
                trigger_ref_id=result.feedback_id,
                content=FEEDBACK_RECEIVED_REPLY,
            )
        elif category == "gray_enroll" and reader_id:
            already_static = reader_id in self.auto_reply_allowlist
            newly_added = bool(result.allowlist_enrolled) and not already_static
            _LOGGER.info(
                "Support gray allowlist processed: seq=%d newly_added=%s",
                result.seq,
                str(newly_added).lower(),
            )
            await self._deliver_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind="gray_enroll",
                trigger_ref_id=result.seq,
                content=GRAY_ENROLL_ADDED_REPLY if newly_added else GRAY_ENROLL_ALREADY_REPLY,
            )
        elif category == "question" and result.batch is not None:
            if result.batch.status == "open":
                self.notify_scheduler()
            elif result.batch.status == "processing":
                if result.preempted_batch_id is not None:
                    self._cancel_batch_task(result.preempted_batch_id)
                self._spawn_immediate_task(result.batch.batch_id, result.batch.generation)

    async def flush_due_now(self) -> list[FlushOutcome]:
        """Flush all open debounce batches whose deadline has arrived and run downstream hooks."""
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        async with self._flush_lock:
            outcomes = await asyncio.to_thread(self.store.flush_due_batches)
            for outcome in outcomes:
                await self._dispatch_flushed_outcome(outcome, run_triage=True)
            return outcomes

    async def wait_for_idle(self, timeout: float = 5.0) -> None:
        """Await any currently running immediate generation tasks (primarily for testing/shutdown)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._active_tasks:
            tasks = [task for _, task in self._active_tasks.values()]
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            if remaining == 0.0:
                raise asyncio.TimeoutError("Timed out waiting for immediate tasks")
            await asyncio.wait(tasks, timeout=remaining)

    def _cancel_batch_task(self, batch_id: int) -> None:
        existing = self._active_tasks.get(batch_id)
        if existing is not None:
            _, task = existing
            if not task.done():
                task.cancel()

    def _spawn_immediate_task(self, batch_id: int, expected_generation: int) -> None:
        if self._loop is None or self._loop.is_closed() or self._stop_requested:
            return
        self._cancel_batch_task(batch_id)
        task = self._loop.create_task(
            self._run_immediate_task(batch_id, expected_generation),
            name=f"support-immediate-{batch_id}-g{expected_generation}",
        )
        self._active_tasks[batch_id] = (expected_generation, task)

    async def _run_triage(self, outcome: FlushOutcome) -> Any:
        if outcome.route_stage == "ready_for_triage" and self.triage_fn is not None:
            res = self.triage_fn(outcome)
            if inspect.isawaitable(res):
                return await res
            return res
        return None

    async def _run_immediate_task(self, batch_id: int, expected_generation: int) -> None:
        try:
            prepared = await asyncio.to_thread(
                self.store.prepare_immediate_generation, batch_id, expected_generation
            )
            if prepared is None:
                return

            triage_result: Any = None
            try:
                triage_result = await self._run_triage(prepared)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning(
                    "Support immediate triage failed: batch_id=%d gen=%d reason=%s",
                    batch_id,
                    expected_generation,
                    type(exc).__name__,
                )

            committed, promoted = await asyncio.to_thread(
                self.store.commit_batch_generation,
                batch_id,
                expected_generation,
                route_stage=prepared.route_stage,
                staff_case_id=prepared.staff_case_id,
                same_case_reason=prepared.same_case_reason,
            )
            if committed is None:
                _LOGGER.info(
                    "Support stale generation discarded before commit: batch_id=%d gen=%d",
                    batch_id,
                    expected_generation,
                )
                return

            await self._dispatch_flushed_outcome(
                committed, run_triage=False, triage_result=triage_result
            )
            if promoted is not None:
                self._spawn_immediate_task(promoted.batch_id, promoted.generation)
        except asyncio.CancelledError:
            _LOGGER.info(
                "Support in-flight generation preempted: batch_id=%d gen=%d",
                batch_id,
                expected_generation,
            )
            raise
        finally:
            tracked = self._active_tasks.get(batch_id)
            if tracked is not None and tracked[0] == expected_generation:
                self._active_tasks.pop(batch_id, None)

    async def _scheduler_loop(self) -> None:
        assert self._wakeup is not None
        while not self._stop_requested:
            try:
                await self.flush_due_now()
            except Exception as exc:
                _LOGGER.warning("Support scheduler flush failed: reason=%s", type(exc).__name__)

            if self._stop_requested:
                break

            now_epoch = self.store.now()
            if (
                self.purge_interval_seconds > 0
                and now_epoch - self._last_purge_epoch >= self.purge_interval_seconds
            ):
                try:
                    await self.purge_expired_now()
                except Exception as exc:
                    _LOGGER.warning("Support scheduler expiry purge failed: reason=%s", type(exc).__name__)
                self._last_purge_epoch = self.store.now()
                now_epoch = self._last_purge_epoch

            if self._stop_requested:
                break

            next_deadline = self.store.next_open_deadline()
            delay: float | None = None
            if next_deadline is not None:
                delay = max(0.0, next_deadline - now_epoch)
            if self.purge_interval_seconds > 0:
                next_purge_delay = max(
                    0.0, (self._last_purge_epoch + self.purge_interval_seconds) - now_epoch
                )
                delay = next_purge_delay if delay is None else min(delay, next_purge_delay)

            if delay is None:
                await self._wakeup.wait()
                self._wakeup.clear()
                continue

            if delay == 0.0:
                continue
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
                self._wakeup.clear()
            except asyncio.TimeoutError:
                pass

    async def _dispatch_flushed_outcome(
        self, outcome: FlushOutcome, *, run_triage: bool, triage_result: Any = None
    ) -> None:
        _LOGGER.info(
            "Support batch flushed: batch_id=%d count=%d gen=%d preempts=%d route=%s case_id=%s reason=%s",
            outcome.batch.batch_id,
            outcome.batch.message_count,
            outcome.batch.generation,
            outcome.batch.preempt_count,
            outcome.route_stage,
            outcome.staff_case_id if outcome.staff_case_id is not None else "-",
            _safe_token(outcome.same_case_reason) or "-",
        )
        if run_triage:
            try:
                triage_result = await self._run_triage(outcome)
            except Exception as exc:
                _LOGGER.warning(
                    "Support batch triage failed: batch_id=%d reason=%s",
                    outcome.batch.batch_id,
                    type(exc).__name__,
                )
        if self.commit_handler is not None:
            try:
                commit_res = self.commit_handler(outcome, triage_result)
                if inspect.isawaitable(commit_res):
                    await commit_res
            except Exception as exc:
                _LOGGER.warning(
                    "Support batch commit handler failed: batch_id=%d reason=%s",
                    outcome.batch.batch_id,
                    type(exc).__name__,
                )
        if self.on_batch_flushed is not None:
            try:
                hook_result = self.on_batch_flushed(outcome)
                if inspect.isawaitable(hook_result):
                    await hook_result
            except Exception as exc:
                _LOGGER.warning(
                    "Support batch post-flush hook failed: batch_id=%d reason=%s",
                    outcome.batch.batch_id,
                    type(exc).__name__,
                )

    async def deliver_reply(
        self,
        *,
        account_id: str,
        reader_id: str,
        trigger_kind: str,
        trigger_ref_id: int | None,
        content: str,
    ) -> int:
        return await self._deliver_reply(
            account_id=account_id,
            reader_id=reader_id,
            trigger_kind=trigger_kind,
            trigger_ref_id=trigger_ref_id,
            content=content,
        )

    async def deliver_staff_reply(
        self,
        *,
        account_id: str,
        reader_id: str,
        trigger_ref_id: int | None,
        content: str,
        operator_username: str = "",
        trigger_kind: str = "staff_dashboard_reply",
    ) -> dict[str, Any]:
        """Send an explicit librarian reply directly via custom/send, bypassing auto-reply allowlist gating."""
        if self.send_text_fn is None:
            reply_id = self.store.record_outbound_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind=trigger_kind,
                trigger_ref_id=trigger_ref_id,
                content=content,
                delivery_mode="direct_https",
                status="failed",
                errmsg="send_client_unavailable",
                operator_username=operator_username,
            )
            return {
                "ok": False,
                "reply_id": reply_id,
                "errcode": None,
                "errmsg": "send_client_unavailable",
            }
        try:
            result = await asyncio.to_thread(self.send_text_fn, reader_id, content)
        except Exception as exc:
            reply_id = self.store.record_outbound_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind=trigger_kind,
                trigger_ref_id=trigger_ref_id,
                content=content,
                delivery_mode="direct_https",
                status="failed",
                errmsg=type(exc).__name__,
                operator_username=operator_username,
            )
            _LOGGER.warning(
                "Staff dashboard reply failed: reply_id=%d ref_id=%s reason=%s",
                reply_id,
                trigger_ref_id if trigger_ref_id is not None else "-",
                type(exc).__name__,
            )
            return {
                "ok": False,
                "reply_id": reply_id,
                "errcode": None,
                "errmsg": type(exc).__name__,
            }

        ok = bool(result.get("ok"))
        errcode = result.get("errcode") if type(result.get("errcode")) is int else None
        errmsg = _safe_errmsg(result.get("errmsg"))
        duration_ms = result.get("duration_ms") if type(result.get("duration_ms")) is int else None
        delivery_mode = result.get("mode") if isinstance(result.get("mode"), str) else "direct_https"
        reply_id = self.store.record_outbound_reply(
            account_id=account_id,
            reader_id=reader_id,
            trigger_kind=trigger_kind,
            trigger_ref_id=trigger_ref_id,
            content=content,
            delivery_mode=delivery_mode,
            status="sent" if ok else "failed",
            errcode=errcode,
            errmsg=errmsg,
            duration_ms=duration_ms,
            operator_username=operator_username,
        )
        _LOGGER.info(
            "Staff dashboard reply finished: reply_id=%d ref_id=%s ok=%s errcode=%s duration_ms=%s",
            reply_id,
            trigger_ref_id if trigger_ref_id is not None else "-",
            str(ok).lower(),
            errcode if errcode is not None else "-",
            duration_ms if duration_ms is not None else "-",
        )
        return {
            "ok": ok,
            "reply_id": reply_id,
            "errcode": errcode,
            "errmsg": errmsg,
            "duration_ms": duration_ms,
        }

    async def _deliver_reply(
        self,
        *,
        account_id: str,
        reader_id: str,
        trigger_kind: str,
        trigger_ref_id: int | None,
        content: str,
    ) -> int:
        shadow_reason: str | None = None
        if not self.auto_reply_enabled or self.send_text_fn is None:
            shadow_reason = "disabled"
        elif (
            self.auto_reply_allowlist or self.gray_enroll_keyword
        ) and reader_id not in self.auto_reply_allowlist:
            in_dynamic = await asyncio.to_thread(
                self.store.is_allowlisted, account_id, reader_id
            )
            if not in_dynamic:
                shadow_reason = "not_in_allowlist"

        if shadow_reason is not None:
            reply_id = self.store.record_outbound_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind=trigger_kind,
                trigger_ref_id=trigger_ref_id,
                content=content,
                delivery_mode="shadow",
                status="shadow",
            )
            _LOGGER.info(
                "Support reply recorded in shadow mode: reply_id=%d trigger=%s ref_id=%s reason=%s",
                reply_id,
                trigger_kind,
                trigger_ref_id if trigger_ref_id is not None else "-",
                shadow_reason,
            )
            return reply_id

        try:
            result = await asyncio.to_thread(self.send_text_fn, reader_id, content)
        except Exception as exc:
            reply_id = self.store.record_outbound_reply(
                account_id=account_id,
                reader_id=reader_id,
                trigger_kind=trigger_kind,
                trigger_ref_id=trigger_ref_id,
                content=content,
                delivery_mode="direct_https",
                status="failed",
                errmsg=type(exc).__name__,
            )
            _LOGGER.warning(
                "Support reply failed: reply_id=%d trigger=%s reason=%s",
                reply_id,
                trigger_kind,
                type(exc).__name__,
            )
            return reply_id

        ok = bool(result.get("ok"))
        errcode = result.get("errcode") if type(result.get("errcode")) is int else None
        errmsg = _safe_errmsg(result.get("errmsg"))
        duration_ms = result.get("duration_ms") if type(result.get("duration_ms")) is int else None
        delivery_mode = result.get("mode") if isinstance(result.get("mode"), str) else "direct_https"
        reply_id = self.store.record_outbound_reply(
            account_id=account_id,
            reader_id=reader_id,
            trigger_kind=trigger_kind,
            trigger_ref_id=trigger_ref_id,
            content=content,
            delivery_mode=delivery_mode,
            status="sent" if ok else "failed",
            errcode=errcode,
            errmsg=errmsg,
            duration_ms=duration_ms,
        )
        _LOGGER.info(
            "Support reply finished: reply_id=%d trigger=%s ok=%s errcode=%s duration_ms=%s",
            reply_id,
            trigger_kind,
            str(ok).lower(),
            errcode if errcode is not None else "-",
            duration_ms if duration_ms is not None else "-",
        )
        return reply_id
