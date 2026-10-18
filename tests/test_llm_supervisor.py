"""LLM-supervisor coverage — a scripted model through the real graph.

The stub path drives the Supervisor with ``rule_based_decider``; these
tests instead route the **LLM decider** (``make_llm_decider``) over a
``ScriptedLLM`` whose queued ``SupervisorRoute`` answers include garbage:
unknown workers, empty fan-outs, oversized fan-outs, raised errors.
Every scenario must still be clamped by the Supervisor's hard guards and
terminate inside the dispatch bound.
"""

import asyncio
import random
from collections import deque
from collections.abc import Iterable
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.framework.registry import FINISH
from app.framework.supervisor import DispatchSpec, RoutingDecision
from app.graph.deciders import SupervisorRoute
from app.graph.graph import build_graph, initial_state
from app.graph.state import Brief
from app.services.prompt_registry import PromptRegistry
from tests.test_graph import _FakeScraper, _FakeSearch, _settings, _stub_llm

_ScriptItem = RoutingDecision | Exception | TimeoutError


class ScriptedLLM:
    """``StructuredLLM`` whose supervisor answers come from a queue.

    Routing-decision schemas (``SupervisorRoute``) pop the next scripted
    item — a decision to return or an exception to raise (the "timeout"
    script entry raises ``TimeoutError``; the Supervisor must clamp it
    like any decider failure). Anything else delegates to the same
    deterministic stub builders the offline path uses, so the workers
    still produce valid plans, drafts and verdicts.
    """

    model_name = "scripted"

    def __init__(
        self,
        script: Iterable[_ScriptItem] = (),
        *,
        fallback: _ScriptItem | None = None,
        verdict: str = "approve",
    ) -> None:
        self._inner = _stub_llm(verdict=verdict)
        self._script: deque[_ScriptItem] = deque(script)
        self._fallback = fallback
        self.supervisor_calls = 0

    async def structured(
        self,
        schema: type[Any],
        *,
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
    ) -> Any:
        if issubclass(schema, RoutingDecision):
            self.supervisor_calls += 1
            item = (
                self._script.popleft()
                if self._script
                else (self._fallback if self._fallback is not None else SupervisorRoute())
            )
            if isinstance(item, BaseException):
                raise item
            return item
        return await self._inner.structured(
            schema, system=system, user=user, context=context
        )


def _route(worker: str = FINISH, reason: str = "s") -> SupervisorRoute:
    return SupervisorRoute(next_worker=worker, reason=reason)


def _graph(
    settings: Settings, llm: ScriptedLLM, prompts: PromptRegistry
) -> Any:
    # `decider` omitted: ScriptedLLM is not a StubLLM, so the graph wires
    # the real make_llm_decider path — prompt render, tracked call and all.
    return build_graph(
        settings,
        llm=llm,  # type: ignore[arg-type]
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
        prompts=prompts,
    )


async def _run(
    g: Any, brief_kwargs: dict[str, object], job_id: str, **kw: Any
) -> dict[str, Any]:
    state = initial_state(
        job_id=job_id,
        workspace_id="ws-a",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=2,
        **kw,
    )
    return await g.ainvoke(state, config={"configurable": {"thread_id": job_id}})


async def test_valid_sequence_reaches_assembly(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """(a) researcher → writer → reviewer, approve → report assembles."""
    llm = ScriptedLLM([_route("researcher"), _route("writer"), _route("reviewer")])
    out = await _run(_graph(_settings(), llm, prompts), brief_kwargs, "ls-ok")
    assert out["status"] == "done"
    assert out["final_report"], "approved draft must assemble a report"
    dispatches = [
        m.recipient
        for m in out["transcript"]
        if m.kind == "dispatch" and m.sender == "supervisor"
    ]
    assert dispatches == ["researcher", "writer", "reviewer", FINISH]


async def test_illegal_route_is_clamped_to_finish(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """(b) A route to a nonexistent worker is clamped, never dispatched."""
    llm = ScriptedLLM([_route("definitely_not_a_worker")])
    out = await _run(_graph(_settings(), llm, prompts), brief_kwargs, "ls-bad")
    assert out["status"] in ("awaiting_review", "done", "failed")
    clamp = next(
        m
        for m in out["transcript"]
        if m.kind == "dispatch" and m.sender == "supervisor"
    )
    assert clamp.recipient == FINISH
    assert "illegal route" in clamp.content


async def test_decider_exception_finishes_without_crashing(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """(c) A raising decider — including a provider timeout — never crashes."""
    llm = ScriptedLLM([RuntimeError("provider exploded"), TimeoutError("timed out")])
    out = await _run(_graph(_settings(), llm, prompts), brief_kwargs, "ls-exc")
    assert out["status"] in ("awaiting_review", "done", "failed")
    assert llm.supervisor_calls == 1  # first raise ends routing
    clamp = next(
        m
        for m in out["transcript"]
        if m.kind == "dispatch" and m.sender == "supervisor"
    )
    assert clamp.recipient == FINISH and "decider error" in clamp.content


async def test_fanout_is_pruned_to_legal_targets_within_budget(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """(d) Fan-out keeps only team members, bounded by remaining budget."""
    llm = ScriptedLLM(
        [
            SupervisorRoute(
                dispatches=[
                    DispatchSpec(worker="researcher"),
                    DispatchSpec(worker="not_a_worker"),
                    DispatchSpec(worker="writer"),
                    DispatchSpec(worker="reviewer"),
                    DispatchSpec(worker="also_fake"),
                ]
            ),
        ]
    )
    out = await _run(
        _graph(_settings(supervisor_max_dispatches=4), llm, prompts),
        brief_kwargs,
        "ls-fan",
    )
    first = next(
        m
        for m in out["transcript"]
        if m.kind == "dispatch" and m.sender == "supervisor"
    )
    assert first.recipient in {"researcher", "writer", "reviewer"}
    sent = {
        m.recipient
        for m in out["transcript"]
        if m.kind == "dispatch" and m.sender == "supervisor"
    }
    assert "not_a_worker" not in sent and "also_fake" not in sent


async def test_supervisor_prompt_version_recorded(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """(e) The LLM decider's prompt version lands in state provenance."""
    llm = ScriptedLLM([_route("researcher"), _route(FINISH)])
    out = await _run(_graph(_settings(), llm, prompts), brief_kwargs, "ls-pv")
    assert out["prompt_versions"]["supervisor"] == prompts.get("supervisor").version


async def test_never_finishing_decider_hits_dispatch_bound(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """(f) A decider that never says FINISH terminates at the bound."""
    llm = ScriptedLLM(fallback=_route("researcher"))
    settings = _settings(supervisor_max_dispatches=5, max_worker_dispatches=20)
    out = await _run(_graph(settings, llm, prompts), brief_kwargs, "ls-loop")
    assert out["status"] in ("awaiting_review", "done", "failed")
    assert out["dispatches"] <= settings.supervisor_max_dispatches
    assert any(
        "dispatch budget exhausted" in m.content
        for m in out["transcript"]
        if m.sender == "supervisor"
    )


_TERMINAL = {"done", "awaiting_review", "failed", "blocked"}


async def test_random_decision_streams_always_terminate(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """200 seeded runs of valid/invalid decisions never hang or overspend."""
    rng = random.Random(14)
    settings = _settings(
        supervisor_max_dispatches=8, max_worker_dispatches=20, max_debate_rounds=2
    )
    workers = ["researcher", "writer", "reviewer", "bogus_worker", ""]
    llm = ScriptedLLM()
    g = _graph(settings, llm, prompts)

    def random_item() -> _ScriptItem:
        kind = rng.randrange(10)
        if kind < 6:
            return _route(rng.choice(workers))
        if kind < 8:
            return SupervisorRoute(
                dispatches=[
                    DispatchSpec(worker=rng.choice(workers))
                    for _ in range(rng.randrange(5))
                ]
            )
        return rng.choice([RuntimeError("x"), TimeoutError("t")])

    for i in range(200):
        llm._script.clear()
        llm._script.extend(random_item() for _ in range(rng.randrange(1, 12)))
        llm._fallback = _route(rng.choice(workers)) if rng.random() < 0.4 else None
        out = await asyncio.wait_for(
            _run(g, brief_kwargs, f"ls-rand-{i}"), timeout=30
        )
        assert out["status"] in _TERMINAL, f"run {i}: {out['status']}"
        assert out["dispatches"] <= settings.supervisor_max_dispatches

