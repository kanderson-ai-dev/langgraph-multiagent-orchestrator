"""Full-graph tests: end-to-end run, bounded debate, guaranteed termination."""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult

_HTML = (
    b"<html><title>Src</title><body>RAG systems retrieve documents "
    b"to ground generation in sources.</body></html>"
)


class _FakeSearch:
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://src.test/a", snippet="s")]


class _FakeScraper:
    async def scrape(self, url: str) -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html", body=_HTML
        )


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


def _stub_llm(*, verdict: str = "approve") -> StubLLM:
    stub = StubLLM()
    stub.register(
        "ResearchPlan",
        lambda s, c: ResearchPlan(
            sub_questions=["q1"], queries=["rag adoption"]
        ),
    )
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="Stub Report",
            markdown="# Stub\nContent grounded in sources.",
            citations=[
                DraftCitation(
                    claim="RAG grounds generation",
                    quote="RAG systems retrieve documents",
                    source_url="https://src.test/a",
                )
            ],
        ),
    )
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict=verdict,
            score=4.2,
            rubric={
                "structure": 4.2, "clarity": 4.2, "grounding": 4.2, "tone": 4.2
            },
            feedback=[],
        ),
    )
    return stub


def _graph(settings: Settings, llm: StubLLM) -> Any:
    return build_graph(
        settings,
        llm=llm,
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )


async def test_end_to_end_approval(brief_kwargs: dict[str, object]) -> None:
    settings = _settings()
    g = _graph(settings, _stub_llm(verdict="approve"))
    state = initial_state(
        job_id="j1", workspace_id="ws-a",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=settings.max_debate_rounds,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "j1"}})
    assert out["status"] == "done"
    assert out["final_report"] and "Stub Report" in out["final_report"]
    assert "## Sources" in out["final_report"]
    senders = [m.sender for m in out["transcript"]]
    assert {"input_guardrail", "researcher", "writer", "reviewer", "supervisor"} <= set(
        senders
    )


async def test_debate_revise_then_escalate_bounded(brief_kwargs: dict[str, object]) -> None:
    """Reviewer never approves → debate exhausts rounds → human_review, never
    infinite. Guaranteed termination under worst-case verdicts."""
    settings = _settings(max_debate_rounds=2)
    g = _graph(settings, _stub_llm(verdict="revise"))
    state = initial_state(
        job_id="j2", workspace_id="ws-a",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=settings.max_debate_rounds,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "j2"}})
    assert out["status"] == "awaiting_review"
    assert out["debate_round"] <= 2
    # Each revise round adds a writer draft; bounded by max_rounds.
    assert len(out["drafts"]) <= 3
    verdicts = [m for m in out["transcript"] if m.kind == "verdict"]
    assert len(verdicts) >= 2
    assert out["transcript"][-1].kind == "escalation"


async def test_max_dispatches_bound(brief_kwargs: dict[str, object]) -> None:
    """Even if routing misbehaves, the dispatch budget bounds the run."""
    settings = _settings(max_debate_rounds=10)
    g = _graph(settings, _stub_llm(verdict="revise"))
    state = initial_state(
        job_id="j3", workspace_id="ws-a",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=settings.max_debate_rounds,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "j3"}})
    # Terminated — never hangs.
    assert out["status"] in ("awaiting_review", "done", "failed")


async def test_per_worker_dispatch_cap_stops_research_loop(
    brief_kwargs: dict[str, object],
) -> None:
    """A decider stuck on "gather more evidence" cannot loop a worker:
    after `max_worker_dispatches` runs it leaves the Supervisor's catalog
    and routing falls through to FINISH."""
    from app.framework.supervisor import RoutingDecision

    async def stuck_decider(state: dict[str, Any], registry: Any) -> RoutingDecision:
        return RoutingDecision(next_worker="researcher", reason="more evidence")

    settings = _settings(max_worker_dispatches=2)
    g = build_graph(
        settings,
        llm=_stub_llm(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
        decider=stuck_decider,
    )
    state = initial_state(
        job_id="j4", workspace_id="ws-a",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=settings.max_debate_rounds,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "j4"}})
    researcher_runs = [
        m for m in out["transcript"]
        if m.kind == "dispatch" and m.recipient == "researcher"
    ]
    assert len(researcher_runs) == settings.max_worker_dispatches
    # Once the cap hit, the supervisor finished instead of looping.
    assert out["status"] in ("done", "failed")
