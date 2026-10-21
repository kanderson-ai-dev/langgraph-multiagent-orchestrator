"""Cost tracking — per-job, per-agent-role token/cost accounting.

A context-scoped ``UsageTracker`` is bound to a job at submission time; every
LLM call records tokens and derives USD cost from configurable per-1M prices.
The tracker is a ``contextvars.ContextVar`` so async tasks (parallel workers)
each see their own binding.
"""

import contextvars
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import LLM_CALLS_TOTAL, LLM_COST_USD, LLM_TOKENS_TOTAL
from app.services.store_protocols import UsageStoreProtocol

logger = get_logger(__name__)

_current: contextvars.ContextVar["UsageTracker | None"] = contextvars.ContextVar(
    "usage_tracker", default=None
)

# ─── model price table ────────────────────────────────────────────────────────
# USD per 1M tokens: (input, output). Bumped whenever a model is added or a
# price changes — the stamp surfaces on the dashboard so cost numbers are
# attributable to a specific price sheet. Models not listed fall back to the
# configurable COST_*_PRICE_PER_1M settings.
PRICE_TABLE_VERSION = "2026-10"

MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}


def price_for(model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M for ``model`` — None when unlisted."""
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    # Prefix match covers dated variants ("gpt-4o-mini-2024-07-18").
    for name, prices in MODEL_PRICES.items():
        if model.startswith(name + "-"):
            return prices
    return None


# ─── pre-call worst-case estimation ──────────────────────────────────────────
# The between-call gate only sees *accrued* cost, so a single in-flight call
# could overrun the cap by its own size. Before every metered call we compute
# the worst case — prompt tokens (tiktoken, never below the chars/4 stub
# convention) + the configured max completion tokens, priced per-1M, times
# (1 + max_retries) since a retry re-bills the full attempt — and refuse when
# it cannot fit the remaining budget. When a smaller completion budget still
# fits, the call runs with ``max_completion_tokens`` clamped instead.
DEFAULT_MAX_COMPLETION_TOKENS = 4096
MIN_VIABLE_COMPLETION_TOKENS = 256


class BudgetExceededError(RuntimeError):
    """Refusal: this call's worst-case estimate cannot fit the budget."""

    def __init__(
        self,
        *,
        role: str,
        estimated_usd: float,
        remaining_usd: float,
        budget_usd: float,
    ) -> None:
        self.role = role
        self.estimated_usd = estimated_usd
        self.remaining_usd = remaining_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"{role} call refused: worst-case ${estimated_usd:.6f} exceeds "
            f"remaining budget ${remaining_usd:.6f} (cap ${budget_usd:.4f})"
        )


@dataclass(frozen=True)
class CallEstimate:
    prompt_tokens: int
    max_completion_tokens: int
    attempts: int  # 1 + max_retries — every retry re-bills a full attempt
    input_price_per_1m: float
    output_price_per_1m: float
    worst_case_usd: float
    priced: bool  # False → model unpriced; configured defaults were used


def _count_tokens(system: str, user: str, model: str | None) -> int:
    """Prompt token count — tiktoken when available, but never below the
    chars/4 convention the stub reports (the estimate must stay a ceiling)."""
    chars4 = (len(system) + len(user)) // 4
    counted = 0
    try:
        import tiktoken

        try:
            enc = tiktoken.encoding_for_model(model or "gpt-4o-mini")
        except (KeyError, ValueError):
            enc = tiktoken.get_encoding("cl100k_base")
        counted = len(enc.encode(system)) + len(enc.encode(user))
    except Exception:
        counted = 0
    return max(counted, chars4)


def estimate_call_cost(
    llm: Any,
    *,
    model: str,
    system: str,
    user: str,
    settings: Settings,
) -> CallEstimate:
    """Worst-case USD for one metered call.

    ``(prompt × input_price + max_completion × output_price) × attempts``;
    ``attempts`` = ``1 + llm.max_retries`` because a provider retry re-bills
    the full request. Unpriced models use the configured default prices —
    an estimate is never "free".
    """
    prompt_tokens = _count_tokens(system, user, model or None)
    max_completion = settings.llm_max_tokens or DEFAULT_MAX_COMPLETION_TOKENS
    prices = price_for(model)
    priced = prices is not None
    if prices is None:
        prices = (
            settings.cost_input_price_per_1m,
            settings.cost_output_price_per_1m,
        )
        logger.warning(
            "llm_cost_estimate_unpriced_model",
            model=model or "unknown",
            note="falling back to configured COST_*_PRICE_PER_1M defaults",
        )
    in_p, out_p = prices
    attempts = 1 + int(getattr(llm, "max_retries", 0) or 0)
    attempt = prompt_tokens / 1e6 * in_p + max_completion / 1e6 * out_p
    return CallEstimate(
        prompt_tokens=prompt_tokens,
        max_completion_tokens=max_completion,
        attempts=attempts,
        input_price_per_1m=in_p,
        output_price_per_1m=out_p,
        worst_case_usd=attempt * attempts,
        priced=priced,
    )


def _affordable_completion_tokens(est: CallEstimate, remaining: float) -> int:
    """Largest completion budget whose *full* worst case still fits
    ``remaining`` — negative when even a zero-output attempt can't."""
    if est.output_price_per_1m <= 0:
        return est.max_completion_tokens
    per_attempt = remaining / est.attempts
    headroom = per_attempt * 1e6 - est.prompt_tokens * est.input_price_per_1m
    return math.floor(headroom / est.output_price_per_1m)


class UsageRecord(BaseModel):
    role: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    prompt_name: str | None = None
    prompt_version: int | None = None
    prompt_sha256: str | None = None


class UsageTracker:
    """Accumulates per-role usage for one orchestration job."""

    def __init__(
        self,
        *,
        job_id: str,
        workspace_id: str,
        store: UsageStoreProtocol,
        settings: Settings,
        base_cost: float = 0.0,
        budget_usd: float | None = None,
    ) -> None:
        self.job_id = job_id
        self.workspace_id = workspace_id
        self._store = store
        self._settings = settings
        # Cost recorded before a HITL resume — keeps totals monotonic.
        self._base_cost = base_cost
        # Hard LLM-spend cap for the pre-call worst-case check; None = no
        # cap, 0 = zero cap (every paid call refused).
        self.budget_usd = budget_usd
        self.records: list[UsageRecord] = []

    def compute_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str | None = None,
    ) -> float:
        """Cost in USD — model price table when listed, else the
        configurable default prices."""
        in_p, out_p = (model and price_for(model)) or (
            self._settings.cost_input_price_per_1m,
            self._settings.cost_output_price_per_1m,
        )
        return prompt_tokens / 1_000_000 * in_p + completion_tokens / 1_000_000 * out_p

    async def record_call(
        self,
        *,
        role: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float = 0.0,
        prompt_name: str | None = None,
        prompt_version: int | None = None,
        prompt_sha256: str | None = None,
    ) -> UsageRecord:
        cost = self.compute_cost(prompt_tokens, completion_tokens, model=model)
        rec = UsageRecord(
            role=role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
        )
        self.records.append(rec)
        LLM_TOKENS_TOTAL.labels(role=role, kind="prompt").inc(prompt_tokens)
        LLM_TOKENS_TOTAL.labels(role=role, kind="completion").inc(completion_tokens)
        LLM_COST_USD.labels(role=role).inc(cost)
        LLM_CALLS_TOTAL.labels(
            role=role,
            prompt_version=(
                f"{prompt_name}@v{prompt_version}" if prompt_version else "unknown"
            ),
        ).inc()
        await self._store.record(
            job_id=self.job_id,
            workspace_id=self.workspace_id,
            role=role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
        )
        return rec

    @property
    def total_cost(self) -> float:
        return self._base_cost + sum(r.cost_usd for r in self.records)


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


def current_cost() -> float:
    """Total USD spent so far by the bound job tracker (0 when unbound)."""
    tracker = _current.get()
    return tracker.total_cost if tracker else 0.0


async def tracked_structured(
    llm: Any,
    schema: type[Any],
    *,
    role: str,
    model: str = "",
    system: str,
    user: str,
    context: dict[str, Any] | None = None,
) -> Any:
    """Call ``llm.structured`` and record token/cost usage under ``role``.

    ``model`` may be left empty — it then resolves from the LLM's own
    ``model_name`` attribute (e.g. ``REVIEWER_MODEL`` overrides price as
    themselves, not as the default chat model). Unknown/empty models fall
    back to the configurable COST_*_PRICE_PER_1M settings.

    Real provider token counts ride back on ``context["token_usage"]``
    when the LLM reports them; the stub path estimates honestly
    (chars/4) — estimates are never presented as provider data.
    """
    model = model or str(getattr(llm, "model_name", "") or "")
    # Pre-call worst-case check: refuse calls that provably cannot fit the
    # remaining budget — the between-call gate only sees *accrued* cost.
    tracker = _current.get()
    if tracker is not None and tracker.budget_usd is not None:
        est = estimate_call_cost(
            llm, model=model, system=system, user=user,
            settings=tracker._settings,
        )
        remaining = tracker.budget_usd - tracker.total_cost
        if est.worst_case_usd > remaining:
            affordable = _affordable_completion_tokens(est, remaining)
            if affordable >= MIN_VIABLE_COMPLETION_TOKENS:
                context = context if context is not None else {}
                context["max_completion_tokens"] = min(
                    est.max_completion_tokens, affordable
                )
                logger.info(
                    "llm_completion_budget_clamped",
                    role=role, model=model,
                    max_completion_tokens=context["max_completion_tokens"],
                )
            else:
                raise BudgetExceededError(
                    role=role,
                    estimated_usd=est.worst_case_usd,
                    remaining_usd=remaining,
                    budget_usd=tracker.budget_usd,
                )
    started = time.monotonic()
    result = await llm.structured(schema, system=system, user=user, context=context)
    ctx = context or {}
    usage = ctx.get("token_usage")
    if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
        prompt_tokens = int(usage["prompt_tokens"])
        completion_tokens = int(usage["completion_tokens"] or 0)
        ctx.pop("token_usage", None)
    else:
        prompt_tokens = (len(system) + len(user)) // 4
        completion_tokens = len(str(result)) // 4
    await track_llm_call(
        role=role, model=model,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        started_at=started,
        prompt_name=ctx.get("prompt_name"),
        prompt_version=ctx.get("prompt_version"),
        prompt_sha256=ctx.get("prompt_sha256"),
    )
    return result


async def track_llm_call(
    *,
    role: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    started_at: float | None = None,
    prompt_name: str | None = None,
    prompt_version: int | None = None,
    prompt_sha256: str | None = None,
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
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
    )
