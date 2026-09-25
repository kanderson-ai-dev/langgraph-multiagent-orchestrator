"""Cost tracking — per-job, per-agent-role token/cost accounting.

A context-scoped ``UsageTracker`` is bound to a job at submission time; every
LLM call records tokens and derives USD cost from configurable per-1M prices.
The tracker is a ``contextvars.ContextVar`` so async tasks (parallel workers)
each see their own binding.
"""

import contextvars
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings
from app.core.metrics import LLM_COST_USD, LLM_TOKENS_TOTAL
from app.services.usage_store import UsageStore

_current: contextvars.ContextVar["UsageTracker | None"] = contextvars.ContextVar(
    "usage_tracker", default=None
)


class UsageRecord(BaseModel):
    role: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class UsageTracker:
    """Accumulates per-role usage for one orchestration job."""

    def __init__(
        self, *, job_id: str, workspace_id: str, store: UsageStore, settings: Settings
    ) -> None:
        self.job_id = job_id
        self.workspace_id = workspace_id
        self._store = store
        self._settings = settings
        self.records: list[UsageRecord] = []

    def compute_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1_000_000 * self._settings.cost_input_price_per_1m
            + completion_tokens / 1_000_000 * self._settings.cost_output_price_per_1m
        )

    async def record_call(
        self,
        *,
        role: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float = 0.0,
    ) -> UsageRecord:
        cost = self.compute_cost(prompt_tokens, completion_tokens)
        rec = UsageRecord(
            role=role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        self.records.append(rec)
        LLM_TOKENS_TOTAL.labels(role=role, kind="prompt").inc(prompt_tokens)
        LLM_TOKENS_TOTAL.labels(role=role, kind="completion").inc(completion_tokens)
        LLM_COST_USD.labels(role=role).inc(cost)
        await self._store.record(
            job_id=self.job_id,
            workspace_id=self.workspace_id,
            role=role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        return rec

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.records)


@asynccontextmanager
async def bind_tracker(tracker: UsageTracker) -> AsyncIterator[UsageTracker]:
    """Bind a tracker to the current async context; restores on exit."""
    token = _current.set(tracker)
    try:
        yield tracker
    finally:
        _current.reset(token)


def current_tracker() -> "UsageTracker | None":
    return _current.get()


async def tracked_structured(
    llm: Any,
    schema: type[Any],
    *,
    role: str,
    model: str,
    system: str,
    user: str,
    context: dict[str, Any] | None = None,
) -> Any:
    """Call ``llm.structured`` and record token/cost usage under ``role``.

    Token counts are estimated (chars/4) when the provider doesn't report
    them — the stub path never fabricates usage, it estimates honestly.
    """
    started = time.monotonic()
    result = await llm.structured(schema, system=system, user=user, context=context)
    prompt_tokens = (len(system) + len(user)) // 4
    completion_tokens = len(str(result)) // 4
    await track_llm_call(
        role=role, model=model,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        started_at=started,
    )
    return result


async def track_llm_call(
    *,
    role: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    started_at: float | None = None,
) -> None:
    """Record an LLM call if a tracker is bound (no-op otherwise)."""
    tracker = _current.get()
    if tracker is None:
        return
    latency_ms = (time.monotonic() - started_at) * 1000 if started_at else 0.0
    await tracker.record_call(
        role=role,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )
