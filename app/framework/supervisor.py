"""Supervisor — the dynamic router at the heart of the orchestrator.

Domain-agnostic: given a snapshot of shared state and the registry of
available workers, the Supervisor produces a ``RoutingDecision`` — the name
of the next worker to dispatch, or ``FINISH``.

The decision itself is delegated to a ``RouterDecider`` callable so the
framework stays testable offline: production wiring supplies an LLM-backed
decider (structured output / function calling); tests supply deterministic
stubs.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from app.framework.registry import FINISH, WorkerRegistry


class DispatchSpec(BaseModel):
    """One dispatch inside a parallel fan-out decision."""

    worker: str = Field(description="Registered worker name to dispatch")
    mandate: str = Field(
        default="", description="Scoped mandate for this parallel instance"
    )
    reason: str = Field(default="")


class RoutingDecision(BaseModel):
    """The Supervisor's choice for the next step.

    Either a single hop (``next_worker``) or a parallel fan-out
    (``dispatches``, e.g. two researchers with different mandates — the
    supervisor decides *when* to parallelize, not a hardcoded edge).
    """

    next_worker: str = Field(
        default=FINISH, description="Worker name to dispatch, or FINISH"
    )
    dispatches: list[DispatchSpec] = Field(
        default_factory=list,
        description="Parallel dispatches; when set, next_worker is ignored",
    )
    reason: str = Field(default="", description="Why this worker is needed now")

    @property
    def finished(self) -> bool:
        return not self.dispatches and self.next_worker == FINISH


RouterDecider = Callable[
    [dict[str, Any], WorkerRegistry], Awaitable[RoutingDecision] | RoutingDecision
]


class Supervisor:
    """Routes work among the registered workers, one decision at a time.

    Hard guards (executed regardless of what the decider returns):

    - the decider may only route to a registered worker or ``FINISH`` —
      anything else is clamped to ``FINISH`` so a confused model can never
      invent a node;
    - ``max_dispatches`` bounds the total number of worker dispatches per
      job, so a misbehaving decider cannot loop forever.

    The Supervisor is *stateless*: the dispatch count lives in the shared
    state (``dispatches``), so one compiled graph can serve many concurrent
    jobs safely. Per-job team restriction is done by passing a narrowed
    registry (``WorkerRegistry.view``) to ``decide``.
    """

    def __init__(
        self,
        registry: WorkerRegistry,
        decider: RouterDecider,
        *,
        max_dispatches: int = 25,
    ) -> None:
        self.registry = registry
        self._decider = decider
        self.max_dispatches = max_dispatches

    async def decide(
        self, state: dict[str, Any], registry: WorkerRegistry | None = None
    ) -> RoutingDecision:
        """Ask the decider for the next worker, then clamp to legal targets."""
        allowed = registry or self.registry
        used = int(state.get("dispatches", 0))
        if used >= self.max_dispatches:
            return RoutingDecision(
                next_worker=FINISH,
                reason=f"dispatch budget exhausted ({self.max_dispatches})",
            )
        try:
            decision_or_coro = self._decider(state, allowed)
            decision = (
                await decision_or_coro
                if isinstance(decision_or_coro, Awaitable)
                else decision_or_coro
            )
        except Exception as exc:  # decider failure must never crash the graph
            return RoutingDecision(next_worker=FINISH, reason=f"decider error: {exc!r}")
        if decision.dispatches:
            # Fan-out: keep only legal targets, bounded by remaining budget.
            valid = [d for d in decision.dispatches if d.worker in allowed]
            remaining = self.max_dispatches - used
            valid = valid[: max(remaining, 0)]
            if not valid:
                return RoutingDecision(
                    next_worker=FINISH,
                    reason="all fan-out targets illegal or dispatch budget spent",
                )
            decision.dispatches = valid
            return decision
        if decision.next_worker != FINISH and decision.next_worker not in allowed:
            return RoutingDecision(
                next_worker=FINISH,
                reason=f"illegal route {decision.next_worker!r} clamped to FINISH",
            )
        return decision
