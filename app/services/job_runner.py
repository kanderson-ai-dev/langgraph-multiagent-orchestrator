"""Job runner — executes the compiled graph in the background per job.

Owns the live event stream (per-node updates fanned out to SSE subscribers),
usage tracking, and job-store status transitions. Resume after HITL uses
``Command(resume=...)`` against the same ``thread_id`` checkpoint.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core.config import Settings
from app.core.cost_tracking import UsageTracker, bind_tracker
from app.core.event_bus import EventBus, InMemoryEventBus
from app.core.logging import get_logger
from app.graph.graph import initial_state
from app.graph.state import Brief
from app.services.job_store import JobRecord, JobStore
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.tracing import bind_trace
from app.services.usage_store import UsageStore

logger = get_logger(__name__)


class JobRunner:
    """Background executor + event bus for orchestration jobs."""

    def __init__(
        self,
        graph: CompiledStateGraph[Any],
        job_store: JobStore,
        usage_store: UsageStore,
        settings: Settings,
        prompt_registry: PromptRegistry | None = None,
        scraper: ScraperClient | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._graph = graph
        self._jobs = job_store
        self._usage = usage_store
        self._settings = settings
        self._prompt_registry = prompt_registry
        self._scraper = scraper
        self._bus: EventBus = event_bus or InMemoryEventBus()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_values: dict[str, dict[str, Any]] = {}
        self._trace_ids: dict[str, str] = {}

    @property
    def job_store(self) -> JobStore:
        return self._jobs

    @property
    def usage_store(self) -> UsageStore:
        return self._usage

    @property
    def prompt_registry(self) -> PromptRegistry | None:
        return self._prompt_registry

    async def _emit(self, job_id: str, event: dict[str, Any]) -> None:
        await self._bus.publish(job_id, event)

    async def events(self, job_id: str) -> list[dict[str, Any]]:
        return await self._bus.history(job_id)

    async def stream(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered then live events until the job reaches a terminal state."""
        async for e in self._bus.subscribe(job_id):
            yield e

    async def submit(
        self,
        job_id: str,
        workspace_id: str,
        brief: Brief,
        *,
        trace_id: str | None = None,
    ) -> None:
        """Launch a job in the background."""
        if trace_id:
            self._trace_ids[job_id] = trace_id
        task = asyncio.create_task(self._run(job_id, workspace_id, brief))
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(job_id, None))

    async def resume(
        self, job_id: str, workspace_id: str, decision: dict[str, Any]
    ) -> None:
        """Resume a job paused at ``human_review`` with the human's decision.

        The status flip to ``running`` happens synchronously, before the
        task launches, so a second review POST hitting the API sees a
        non-awaiting status and gets a 409 — no double resume.
        """
        await self._jobs.update_status(workspace_id, job_id, "running")
        task = asyncio.create_task(self._resume(job_id, workspace_id, decision))
        self._tasks[job_id] = task

    async def expire_stale(self, *, now: datetime | None = None) -> list[JobRecord]:
        """Sweep ``awaiting_review`` jobs idle past ``HITL_EXPIRY_HOURS``.

        ``now`` is injectable so the sweep is deterministic under test.
        Expired is a terminal, *auditable* state — the record keeps its
        partial result, escalation reason and cost; nothing is silently
        failed or deleted.
        """
        hours = self._settings.hitl_expiry_hours
        if hours <= 0:
            return []
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(hours=hours)
        stale = await self._jobs.list_stale_awaiting_review(cutoff)
        expired: list[JobRecord] = []
        for job in stale:
            reason = (
                f"awaiting_review exceeded {hours:g}h without a human "
                "decision — expired"
            )
            rec = await self._jobs.mark_expired(
                job.workspace_id, job.job_id, reason=reason
            )
            if rec is None:
                continue
            expired.append(rec)
            await self._emit(
                job.job_id,
                {
                    "node": "hitl_sweep",
                    "status": "expired",
                    "reason": reason,
                    "terminal": True,
                    "cost_usd": rec.cost_usd,
                },
            )
            logger.info(
                "hitl_expired",
                job_id=job.job_id, workspace_id=job.workspace_id,
                idle_hours=hours,
            )
        return expired

    def _config(self, job_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": job_id}}

    async def _run(self, job_id: str, workspace_id: str, brief: Brief) -> None:
        tracker = UsageTracker(
            job_id=job_id, workspace_id=workspace_id,
            store=self._usage, settings=self._settings,
        )
        started = time.monotonic()
        await self._jobs.update_status(workspace_id, job_id, "running")
        try:
            async with bind_tracker(tracker):
                state = initial_state(
                    job_id=job_id, workspace_id=workspace_id, brief=brief,
                    max_debate_rounds=self._settings.max_debate_rounds,
                    budget_usd=(
                        brief.budget_usd
                        if brief.budget_usd is not None
                        else self._settings.default_job_budget_usd
                    ),
                )
                with bind_trace(
                    name=f"job:{job_id}",
                    session_id=job_id,
                    metadata={
                        "job_id": job_id,
                        "workspace_id": workspace_id,
                        "report_type": brief.report_type,
                    },
                    tags=["job"],
                    trace_id=self._trace_ids.get(job_id),
                ) as callbacks:
                    config: RunnableConfig = {
                        **self._config(job_id), "callbacks": callbacks
                    }
                    async for chunk in self._graph.astream(
                        state, config=config,
                        stream_mode=["updates", "values"],
                    ):
                        await self._handle_chunk(job_id, chunk)
        except Exception as exc:
            logger.exception("job_failed", job_id=job_id)
            await self._jobs.update_status(workspace_id, job_id, "failed")
            await self._emit(
                job_id,
                {"node": "runner", "error": str(exc), "terminal": True},
            )
            return
        await self._finalize(job_id, workspace_id, started)

    async def _resume(
        self, job_id: str, workspace_id: str, decision: dict[str, Any]
    ) -> None:
        """Resume a paused job; ``base_cost`` re-seeds pre-pause spend so cost
        accounting and budget enforcement stay monotonic across resume."""
        base = await self._usage.job_cost(job_id)
        tracker = UsageTracker(
            job_id=job_id, workspace_id=workspace_id,
            store=self._usage, settings=self._settings, base_cost=base,
        )
        started = time.monotonic()
        try:
            async with bind_tracker(tracker):
                # Resume rejoins the job's original trace when it exists.
                with bind_trace(
                    name=f"job:{job_id}:resume",
                    session_id=job_id,
                    metadata={"job_id": job_id, "workspace_id": workspace_id},
                    tags=["job", "hitl-resume"],
                    trace_id=self._trace_ids.get(job_id),
                ) as callbacks:
                    config: RunnableConfig = {
                        **self._config(job_id), "callbacks": callbacks
                    }
                    async for chunk in self._graph.astream(
                        Command(resume=decision),
                        config=config,
                        stream_mode=["updates", "values"],
                    ):
                        await self._handle_chunk(job_id, chunk)
        except Exception as exc:
            logger.exception("resume_failed", job_id=job_id)
            await self._emit(
                job_id,
                {"node": "runner", "error": str(exc), "terminal": True},
            )
            return
        await self._finalize(job_id, workspace_id, started)

    async def _handle_chunk(self, job_id: str, chunk: Any) -> None:
        # Multi-mode streams yield (mode, payload) tuples.
        if isinstance(chunk, tuple) and len(chunk) == 2:
            mode, payload = chunk
            if mode == "values" and isinstance(payload, dict):
                self._last_values[job_id] = payload
                return
            if mode != "updates":
                return
            chunk = payload
        if not isinstance(chunk, dict):
            return
        for node, update in chunk.items():
            if node == "__interrupt__":
                # The interrupt payload carries the real escalation reason
                # (budget | debate_rounds) — surface it to SSE subscribers.
                reason = None
                intr = update[0] if isinstance(update, list | tuple) else update
                val = getattr(intr, "value", None)
                if isinstance(val, dict):
                    reason = val.get("reason")
                await self._emit(
                    job_id,
                    {"node": "human_review", "interrupt": True, "reason": reason},
                )
                continue
            msgs = update.get("transcript") if isinstance(update, dict) else None
            await self._emit(
                job_id,
                {
                    "node": node,
                    "status": update.get("status") if isinstance(update, dict) else None,
                    "messages": [
                        {"sender": m.sender, "kind": m.kind, "content": m.content}
                        for m in msgs
                    ]
                    if msgs
                    else [],
                },
            )

    async def _finalize(self, job_id: str, workspace_id: str, started: float) -> None:
        # Prefer the checkpointer snapshot; fall back to the last streamed values
        # so the runner also works on an uncheckpointered graph.
        values: dict[str, Any] = self._last_values.get(job_id, {})
        try:
            snapshot = await self._graph.aget_state(self._config(job_id))
            values = snapshot.values or values
        except ValueError:
            pass
        status = str(values.get("status", "failed"))
        cost = await self._usage.job_cost(job_id)
        # Job-level latency (active segment — HITL pauses excluded).
        elapsed = time.monotonic() - started
        from app.core.latency import LATENCY
        from app.core.metrics import JOB_DURATION_SECONDS

        JOB_DURATION_SECONDS.observe(elapsed)
        LATENCY.observe("job", elapsed)
        if status == "done":
            transcript = list(values.get("transcript", []))
            from app.services.audit import chain_transcript

            await self._jobs.set_result(
                workspace_id,
                job_id,
                {
                    "report": values.get("final_report"),
                    "audit_root": values.get("audit_root"),
                    "audit_entries": [
                        e.model_dump() for e in chain_transcript(transcript)
                    ],
                    "transcript": [
                        m.model_dump(mode="json") for m in transcript
                    ],
                    "debate_rounds": values.get("debate_round", 0),
                    "prompt_versions": values.get("prompt_versions", {}),
                    "evidence_manifest": values.get("evidence_manifest"),
                    "evidence_root": values.get("evidence_root"),
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
                cost_usd=cost,
                status="done",
            )
        elif status == "awaiting_review":
            # Persist why it escalated — the console/poll shows the real
            # cause plus the legal actions for this pause.
            await self._jobs.set_result(
                workspace_id,
                job_id,
                {
                    "escalation_reason": values.get("escalation_reason"),
                    "allowed_actions": values.get("allowed_actions"),
                    "has_draft": bool(values.get("drafts")),
                    "debate_round": values.get("debate_round", 0),
                    "max_debate_rounds": values.get("max_debate_rounds"),
                    "prompt_versions": values.get("prompt_versions", {}),
                    "evidence_manifest": values.get("evidence_manifest"),
                    "evidence_root": values.get("evidence_root"),
                },
                cost_usd=cost,
                status="awaiting_review",
            )
        else:
            # Terminal non-success (failed / blocked): still persist the
            # transcript + audit chain + rejection reason — a rejected job
            # must stay explainable, never silently `failed`.
            transcript = list(values.get("transcript", []))
            from app.services.audit import audit_root as root_of
            from app.services.audit import chain_transcript

            await self._jobs.set_result(
                workspace_id,
                job_id,
                {
                    "report": None,
                    "rejection_reason": values.get("rejection_reason"),
                    "errors": [str(e) for e in values.get("errors", [])],
                    # The report assembler never ran, so seal the chain tip
                    # here — the audit endpoint can still verify.
                    "audit_root": values.get("audit_root")
                    or root_of(transcript),
                    "audit_entries": [
                        e.model_dump() for e in chain_transcript(transcript)
                    ],
                    "transcript": [
                        m.model_dump(mode="json") for m in transcript
                    ],
                    "debate_rounds": values.get("debate_round", 0),
                    "prompt_versions": values.get("prompt_versions", {}),
                },
                cost_usd=cost,
                status=status,  # type: ignore[arg-type]
            )
        # Awaiting review is a pause, not a terminal state — keep the SSE
        # stream open so subscribers see events after the resume.
        terminal = status in ("done", "failed", "blocked", "expired")
        await self._emit(
            job_id,
            {"node": "runner", "status": status,
             "terminal": terminal, "cost_usd": cost},
        )

    async def cancel(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def aclose(self) -> None:
        """Release owned resources (pooled scraper connections)."""
        if self._scraper is not None:
            await self._scraper.aclose()
