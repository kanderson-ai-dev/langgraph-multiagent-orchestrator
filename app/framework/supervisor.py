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


class RoutingDecision(BaseModel):
    """The Supervisor's choice for the next step."""

    next_worker: str = Field(description="Worker name to dispatch, or FINISH")
    reason: str = Field(default="", description="Why this worker is needed now")

    @property
    def finished(self) -> bool:
        return self.next_worker == FINISH


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
        self._dispatches = 0

    async def decide(self, state: dict[str, Any]) -> RoutingDecision:
        """Ask the decider for the next worker, then clamp to legal targets."""
        if self._dispatches >= self.max_dispatches:
            return RoutingDecision(
                next_worker=FINISH,
                reason=f"dispatch budget exhausted ({self.max_dispatches})",
            )
        try:
            decision_or_coro = self._decider(state, self.registry)
            decision = (
                await decision_or_coro
                if isinstance(decision_or_coro, Awaitable)
                else decision_or_coro
            )
        except Exception as exc:  # decider failure must never crash the graph
            return RoutingDecision(next_worker=FINISH, reason=f"decider error: {exc!r}")
        if decision.next_worker != FINISH and decision.next_worker not in self.registry:
            return RoutingDecision(
                next_worker=FINISH,
                reason=f"illegal route {decision.next_worker!r} clamped to FINISH",
            )
        if not decision.finished:
            self._dispatches += 1
        return decision
