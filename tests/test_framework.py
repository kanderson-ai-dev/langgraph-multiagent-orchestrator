"""Framework tests: registry, dynamic supervisor routing, bounded debate."""

from typing import Any

import pytest

from app.framework import (
    FINISH,
    DebateController,
    DebateOutcome,
    RoutingDecision,
    Supervisor,
    WorkerRegistry,
    decide_next_after_verdict,
)
from app.framework.worker import StateUpdate


class _StubWorker:
    def __init__(self, name: str, description: str = "") -> None:
        self._name = name
        self._description = description
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    async def run(self, state: dict[str, Any]) -> StateUpdate:
        self.calls += 1
        return {}


def _registry(*names: str) -> WorkerRegistry:
    reg = WorkerRegistry()
    for n in names:
        reg.register(_StubWorker(n, f"{n} does things"))
    return reg


# ─── Registry ────────────────────────────────────────────────────────────────


def test_registry_rejects_duplicates_and_reserved() -> None:
    reg = _registry("a")
    with pytest.raises(ValueError):
        reg.register(_StubWorker("a"))
    with pytest.raises(ValueError):
        reg.register(_StubWorker(FINISH))


def test_registry_unknown_worker_raises() -> None:
    reg = _registry("a")
    with pytest.raises(KeyError):
        reg.get("ghost")


def test_registry_catalog_renders_descriptions() -> None:
    reg = _registry("researcher", "writer")
    catalog = reg.catalog()
    assert "- researcher:" in catalog and "- writer:" in catalog


# ─── Supervisor routing ──────────────────────────────────────────────────────


async def test_supervisor_routes_to_registered_worker() -> None:
    reg = _registry("researcher", "writer")

    async def decider(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        return RoutingDecision(next_worker="researcher", reason="need evidence")

    sup = Supervisor(reg, decider)
    d = await sup.decide({})
    assert d.next_worker == "researcher"
    assert not d.finished


async def test_supervisor_finish() -> None:
    reg = _registry("researcher")

    async def decider(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        return RoutingDecision(next_worker=FINISH)

    sup = Supervisor(reg, decider)
    assert (await sup.decide({})).finished


async def test_supervisor_clamps_illegal_route() -> None:
    """A confused decider can never invent a node — clamped to FINISH."""
    reg = _registry("researcher")

    async def decider(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        return RoutingDecision(next_worker="nonexistent_agent")

    sup = Supervisor(reg, decider)
    d = await sup.decide({})
    assert d.finished
    assert "clamped" in d.reason


async def test_supervisor_dispatch_budget_bounds_loops() -> None:
    """Guaranteed termination: even a decider that always routes onward stops.
    The dispatch count lives in state — the Supervisor stays stateless and
    safe to share across concurrent jobs."""
    reg = _registry("looper")

    async def always_onward(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        return RoutingDecision(next_worker="looper", reason="again")

    sup = Supervisor(reg, always_onward, max_dispatches=5)
    state: dict[str, Any] = {}
    seen = []
    for _ in range(10):
        d = await sup.decide(state)
        seen.append(d)
        if not d.finished:
            state["dispatches"] = state.get("dispatches", 0) + 1
    assert all(d.next_worker == "looper" for d in seen[:5])
    assert all(d.finished for d in seen[5:])


async def test_supervisor_respects_team_view() -> None:
    """A narrowed registry (the job's team) clamps routes outside the team."""
    reg = _registry("researcher", "writer", "financial_analyst")

    async def wants_specialist(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        return RoutingDecision(next_worker="financial_analyst")

    sup = Supervisor(reg, wants_specialist)
    team = reg.view(["researcher", "writer"])  # specialist not in this team
    d = await sup.decide({}, registry=team)
    assert d.finished and "clamped" in d.reason


async def test_supervisor_decider_error_fails_safe() -> None:
    reg = _registry("w")

    async def broken(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        raise RuntimeError("llm blew up")

    sup = Supervisor(reg, broken)
    assert (await sup.decide({})).finished


# ─── Debate loop ─────────────────────────────────────────────────────────────


def test_debate_approve_finish() -> None:
    d = DebateController(proposer="writer", max_rounds=3)
    d.record_round()
    assert d.decide(approved=True) == FINISH
    assert d.outcome is DebateOutcome.APPROVED


def test_debate_revise_within_budget_returns_proposer() -> None:
    d = DebateController(proposer="writer", max_rounds=3)
    d.record_round()
    assert d.decide(approved=False) == "writer"


def test_debate_exhaustion_escalates() -> None:
    d = DebateController(proposer="writer", max_rounds=2, escalate_to="human_review")
    d.record_round()
    assert d.decide(approved=False) == "writer"  # round budget still open
    d.record_round()
    assert d.decide(approved=False) == "human_review"
    assert d.outcome is DebateOutcome.ESCALATE


def test_debate_terminal_state_is_sticky() -> None:
    d = DebateController(proposer="writer", max_rounds=1)
    d.record_round()
    assert d.decide(approved=False) == "human_review"
    # Once escalated, further calls cannot re-open the debate.
    assert d.decide(approved=False) == "human_review"
    assert d.decide(approved=True) == "human_review"


def test_debate_zero_rounds_escalates_immediately() -> None:
    d = DebateController(proposer="writer", max_rounds=0)
    assert d.exhausted
    assert d.decide(approved=False) == "human_review"


def test_debate_guaranteed_termination_property() -> None:
    """Property-style test: for ANY sequence of verdicts, the loop terminates
    within max_rounds + 1 decisions. This is the guaranteed-termination proof."""
    for max_rounds in (0, 1, 3, 10):
        d = DebateController(proposer="writer", max_rounds=max_rounds)
        steps = 0
        while True:
            steps += 1
            nxt = d.decide(approved=False)  # worst case: never approved
            if nxt != "writer":
                break
            d.record_round()
        assert nxt == "human_review"
        assert steps <= max_rounds + 1


def test_functional_variant() -> None:
    assert decide_next_after_verdict(
        approved=True, rounds_used=0, max_rounds=3, proposer="writer"
    ) == FINISH
    assert decide_next_after_verdict(
        approved=False, rounds_used=1, max_rounds=3, proposer="writer"
    ) == "writer"
    assert decide_next_after_verdict(
        approved=False, rounds_used=3, max_rounds=3, proposer="writer"
    ) == "human_review"
