"""Full-graph tests: end-to-end run, bounded debate, guaranteed termination."""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.writer import (
    DraftCitation,
    DraftOutput,
    DraftOutputPassages,
    PassageCitation,
)
from app.graph.state import Brief
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult
from tests.conftest import ALL_SECTIONS_MARKDOWN

_HTML = (
    b"<html><title>Src</title><body>"
    b"<p>RAG systems retrieve documents to ground generation in sources. "
    b"Retrieval augmented generation keeps answers anchored to fetched "
    b"evidence rather than model memory.</p>"
    b"</body></html>"
)


class _FakeSearch:
    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://src.test/a", snippet="s")]


class _FakeScraper:
    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
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
            markdown=ALL_SECTIONS_MARKDOWN,
            citations=[
                DraftCitation(
                    claim="RAG grounds generation",
                    quote="RAG systems retrieve documents",
                    source_url="https://src.test/a",
                )
            ],
        ),
    )
    # Extractive mode (the shipped default): the single fake source is
    # passage S0-P0 — the marker in the markdown and the entry's
    # passage_id resolve to the same id.
    stub.register(
        "DraftOutputPassages",
        lambda s, c: DraftOutputPassages(
            title="Stub Report",
            markdown=ALL_SECTIONS_MARKDOWN
            + "\n\nRAG grounds generation in retrieved sources [S0-P0].\n",
            citations=[
                PassageCitation(
                    claim="RAG systems retrieve documents to ground "
                    "generation in sources",
                    passage_id="S0-P0",
                )
            ],
        ),
    )
    # "revise" must be a real defect under the rule-derived verdict: the
    # mechanical overrides lift structure/grounding to 5.0, so a 3.0 stub
    # would mean 4.0 / min 3.0 — an approve. 2.5 dims stay blocking.
    score = 4.2 if verdict == "approve" else 2.5
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict=verdict,
            score=score,
            rubric={
                "structure": score, "clarity": score,
                "grounding": score, "tone": score,
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
