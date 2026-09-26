"""Job runner — executes the compiled graph in the background per job.

Owns the live event stream (per-node updates fanned out to SSE subscribers),
usage tracking, and job-store status transitions. Resume after HITL uses
``Command(resume=...)`` against the same ``thread_id`` checkpoint.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core.config import Settings
from app.core.cost_tracking import UsageTracker, bind_tracker
from app.core.logging import get_logger
from app.graph.graph import initial_state
from app.graph.state import Brief
from app.services.job_store import JobStore
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
    ) -> None:
        self._graph = graph
        self._jobs = job_store
        self._usage = usage_store
        self._settings = settings
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_values: dict[str, dict[str, Any]] = {}

    @property
    def job_store(self) -> JobStore:
        return self._jobs

    @property
    def usage_store(self) -> UsageStore:
        return self._usage

    def _emit(self, job_id: str, event: dict[str, Any]) -> None:
        self._events.setdefault(job_id, []).append(event)
        for q in self._queues.get(job_id, []):
            q.put_nowait(event)

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return self._events.get(job_id, [])

    async def stream(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered then live events until the job reaches a terminal state."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues.setdefault(job_id, []).append(q)
        try:
            terminal_seen = False
            for e in self.events(job_id):  # replay history first
                yield e
                terminal_seen = terminal_seen or bool(e.get("terminal"))
            while not terminal_seen:
                event = await q.get()
                yield event
                terminal_seen = bool(event.get("terminal"))
        finally:
            self._queues[job_id].remove(q)

    async def submit(self, job_id: str, workspace_id: str, brief: Brief) -> None:
        """Launch a job in the background."""
        task = asyncio.create_task(self._run(job_id, workspace_id, brief))
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(job_id, None))

    async def resume(
        self, job_id: str, workspace_id: str, decision: dict[str, Any]
    ) -> None:
        """Resume a job paused at ``human_review`` with the human's decision."""
        task = asyncio.create_task(self._resume(job_id, workspace_id, decision))
        self._tasks[job_id] = task

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
                async for chunk in self._graph.astream(
                    state, config=self._config(job_id),
                    stream_mode=["updates", "values"],
                ):
                    self._handle_chunk(job_id, chunk)
        except Exception as exc:
            logger.exception("job_failed", job_id=job_id)
            await self._jobs.update_status(workspace_id, job_id, "failed")
            self._emit(job_id, {"node": "runner", "error": str(exc), "terminal": True})
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
                async for chunk in self._graph.astream(
                    Command(resume=decision),
                    config=self._config(job_id),
                    stream_mode=["updates", "values"],
                ):
                    self._handle_chunk(job_id, chunk)
        except Exception as exc:
            logger.exception("resume_failed", job_id=job_id)
            self._emit(job_id, {"node": "runner", "error": str(exc), "terminal": True})
            return
        await self._finalize(job_id, workspace_id, started)

    def _handle_chunk(self, job_id: str, chunk: Any) -> None:
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
                self._emit(
                    job_id,
                    {"node": "human_review", "interrupt": True, "reason": reason},
                )
                continue
            msgs = update.get("transcript") if isinstance(update, dict) else None
            self._emit(
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
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
                cost_usd=cost,
                status="done",
            )
        elif status == "awaiting_review":
            # Persist why it escalated — the console/poll shows the real cause.
            await self._jobs.set_result(
                workspace_id,
                job_id,
                {
                    "escalation_reason": values.get("escalation_reason"),
                    "debate_round": values.get("debate_round", 0),
                    "max_debate_rounds": values.get("max_debate_rounds"),
                },
                cost_usd=cost,
                status="awaiting_review",
            )
        else:
            await self._jobs.update_status(workspace_id, job_id, status)  # type: ignore[arg-type]
        # Awaiting review is a pause, not a terminal state — keep the SSE
        # stream open so subscribers see events after the resume.
        terminal = status in ("done", "failed", "blocked")
        self._emit(
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
