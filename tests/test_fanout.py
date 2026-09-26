"""Dynamic fan-out — the Supervisor may dispatch parallel worker instances
with distinct mandates via LangGraph ``Send``, and the debate bound still
holds while dispatches are in flight."""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.framework import FINISH, DispatchSpec, RoutingDecision, WorkerRegistry
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult


class _FakeSearch:
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://src.test/a", snippet="s")]


class _FakeScraper:
    async def scrape(self, url: str) -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html",
            body=b"<html><body>fan out evidence text</body></html>",
        )


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


def _llm() -> StubLLM:
    stub = StubLLM()
    stub.register(
        "ResearchPlan",
        lambda s, c: ResearchPlan(sub_questions=["q"], queries=["q1"]),
    )
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="t", markdown="m",
            citations=[DraftCitation(
                claim="c", quote="fan out evidence",
                source_url="https://src.test/a",
            )],
        ),
    )
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.0,
            rubric={"structure": 4.0, "clarity": 4.0, "grounding": 4.0, "tone": 4.0},
            feedback=[],
        ),
    )
    return stub


async def _fanout_then_flow(
    state: dict[str, Any], registry: WorkerRegistry
) -> RoutingDecision:
    """First decision fans out two researcher mandates; then normal flow."""
    if not state.get("evidence"):
        return RoutingDecision(
            dispatches=[
                DispatchSpec(worker="researcher", mandate="academic sources"),
                DispatchSpec(worker="researcher", mandate="industry sources"),
            ],
            reason="parallel evidence gathering",
        )
    if not state.get("drafts"):
        return RoutingDecision(next_worker="writer")
    if state.get("latest_verdict") is None:
        return RoutingDecision(next_worker="reviewer")
    return RoutingDecision(next_worker=FINISH)


async def test_supervisor_fans_out_researchers_in_parallel() -> None:
    settings = _settings()
    g = build_graph(
        settings, llm=_llm(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
        decider=_fanout_then_flow,
    )
    state = initial_state(
        job_id="f1", workspace_id="ws",
        brief=Brief(topic="grid-scale battery storage economics"),
        max_debate_rounds=2,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "f1"}})

    assert out["status"] == "done"
    # Both parallel researcher instances produced evidence.
    assert len(out["evidence"]) == 2
    dispatches = [m for m in out["transcript"] if m.kind == "dispatch"]
    mandates = {m.content for m in dispatches}
    assert any("academic" in c for c in mandates)
    assert any("industry" in c for c in mandates)
    # Two parallel sends + writer + reviewer dispatches were all counted.
    assert out["dispatches"] == 4


async def test_fanout_illegal_targets_are_clamped() -> None:
    """A decider that fans out to a non-team worker is clamped to FINISH."""
    from app.framework import Supervisor

    async def bad_fanout(state: dict[str, Any], r: WorkerRegistry) -> RoutingDecision:
        return RoutingDecision(
            dispatches=[DispatchSpec(worker="ghost_agent", mandate="x")]
        )

    reg = WorkerRegistry()
    sup = Supervisor(reg, bad_fanout)
    d = await sup.decide({})
    assert d.finished


async def test_termination_with_fanout_worst_case() -> None:
    """Even with fan-out decisions, debate rounds and dispatch budget bound
    the run — guaranteed termination still holds."""
    settings = _settings(max_debate_rounds=1)
    stub = _llm()
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="revise",
            score=3.0,
            rubric={"structure": 3.0, "clarity": 3.0, "grounding": 3.0, "tone": 3.0},
            feedback=[],
        ),
    )
    g = build_graph(
        settings, llm=stub,
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
        decider=_fanout_then_flow,
    )
    state = initial_state(
        job_id="f2", workspace_id="ws",
        brief=Brief(topic="worst case fan-out termination"),
        max_debate_rounds=1,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "f2"}})
    # Never hangs: escalates after the bounded debate.
    assert out["status"] == "awaiting_review"
    assert out["debate_round"] <= 1
