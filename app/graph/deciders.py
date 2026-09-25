"""Supervisor deciders — the routing brain behind ``Supervisor.decide``.

Two implementations:

- ``make_llm_decider``: production path — an LLM with structured output picks
  the next worker from the registry catalog (true dynamic routing).
- ``rule_based_decider``: deterministic offline fallback (no credentials) —
  mirrors the expected flow so the whole graph runs end-to-end in CI.
"""

from typing import Any

from app.framework.registry import FINISH, WorkerRegistry
from app.framework.supervisor import RouterDecider, RoutingDecision
from app.graph.prompts import SUPERVISOR_SYSTEM
from app.services.llm_client import StructuredLLM


class SupervisorRoute(RoutingDecision):
    """Structured output schema for the LLM routing decision."""


def _state_summary(state: dict[str, Any]) -> str:
    verdict = state.get("latest_verdict")
    return "\n".join(
        [
            f"topic: {state['brief'].topic}",
            f"evidence items: {len(state.get('evidence', []))}",
            f"drafts: {len(state.get('drafts', []))}",
            f"debate round: {state.get('debate_round', 0)}",
            f"latest verdict: {verdict.verdict if verdict else 'none'}",
        ]
    )


def make_llm_decider(llm: StructuredLLM) -> RouterDecider:
    """LLM-backed decider — the real Supervisor brain."""

    async def decide(state: dict[str, Any], registry: WorkerRegistry) -> RoutingDecision:
        route = await llm.structured(
            SupervisorRoute,
            system=SUPERVISOR_SYSTEM.format(
                catalog=registry.catalog(), state_summary=_state_summary(state)
            ),
            user="Pick the next worker (or FINISH).",
            context={"state_keys": sorted(state)},
        )
        return route

    return decide


# Roles that are always part of a team's backbone; anything else in the
# registry is a specialist analyst that runs once, between evidence and draft.
_CORE = {"researcher", "writer", "reviewer"}


def rule_based_decider(state: dict[str, Any], registry: WorkerRegistry) -> RoutingDecision:
    """Deterministic decider for offline/CI runs — same flow, no LLM spend."""
    if not state.get("evidence") and "researcher" in registry:
        return RoutingDecision(next_worker="researcher", reason="no evidence yet")
    # Specialists run once each, after evidence and before drafting.
    done = {n.analyst for n in state.get("analyst_notes", [])}
    for name in registry.names():
        if name not in _CORE and name not in done:
            return RoutingDecision(
                next_worker=name, reason=f"specialist analysis pending: {name}"
            )
    if not state.get("drafts") and "writer" in registry:
        return RoutingDecision(next_worker="writer", reason="evidence ready, no draft")
    if state.get("latest_verdict") is None and "reviewer" in registry:
        return RoutingDecision(next_worker="reviewer", reason="draft needs audit")
    return RoutingDecision(next_worker=FINISH, reason="flow complete")
