"""Cost-governed autonomy — a spent budget stops paid work structurally,
escalates cleanly, and `fund` resumes the job with a raised cap."""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult

_HTML = b"<html><body>budget evidence text lives here</body></html>"


class _SpySearch:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        self.calls += 1
        return [SearchResult(title="t", url="https://src.test/a", snippet="s")]


class _SpyScraper:
    async def scrape(self, url: str) -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html", body=_HTML
        )


class _SpyLLM(StubLLM):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.register(
            "ResearchPlan",
            lambda s, c: ResearchPlan(sub_questions=["q"], queries=["q1"]),
        )
        self.register(
            "DraftOutput",
            lambda s, c: DraftOutput(
                title="t", markdown="m",
                citations=[DraftCitation(
                    claim="c", quote="budget evidence text",
                    source_url="https://src.test/a",
                )],
            ),
        )
        self.register(
            "ReviewOutput",
            lambda s, c: ReviewOutput(
                verdict="approve",
                score=4.0,
                rubric={
                    "structure": 4.0, "clarity": 4.0, "grounding": 4.0, "tone": 4.0
                },
                feedback=[],
            ),
        )

    async def structured(self, schema: Any, **kw: Any) -> Any:
        self.calls += 1
        return await super().structured(schema, **kw)


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


def _graph(settings: Settings, llm: _SpyLLM, search: _SpySearch) -> Any:
    return build_graph(
        settings, llm=llm,
        search=search,  # type: ignore[arg-type]
        scraper=_SpyScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )


def _state(settings: Settings, budget: float | None) -> dict[str, Any]:
    return initial_state(
        job_id="b1", workspace_id="ws",
        brief=Brief(topic="cost governance smoke test"),
        max_debate_rounds=settings.max_debate_rounds,
        budget_usd=budget,
    )


async def test_zero_budget_burns_zero_llm_calls() -> None:
    """Budget exhausted at $0 → escalate before the decider itself runs."""
    settings = _settings()
    llm, search = _SpyLLM(), _SpySearch()
    g = _graph(settings, llm, search)
    out = await g.ainvoke(
        _state(settings, budget=0.0), config={"configurable": {"thread_id": "b0"}}
    )
    assert out["status"] == "awaiting_review"
    assert out["escalation_reason"] == "budget"
    assert llm.calls == 0 and search.calls == 0
    assert any("budget" in m.content for m in out["transcript"]
               if m.kind == "escalation")


async def test_tiny_budget_stops_mid_flow(tmp_path: Any) -> None:
    """With a bound tracker (as the runner binds in production), the first
    paid call pushes spend over the cap and the next dispatch escalates."""
    from app.core.cost_tracking import UsageTracker, bind_tracker
    from app.services.usage_store import UsageStore

    settings = _settings(database_path=str(tmp_path / "u.sqlite"))
    llm, search = _SpyLLM(), _SpySearch()
    g = _graph(settings, llm, search)
    tracker = UsageTracker(
        job_id="bt", workspace_id="ws",
        store=UsageStore(settings.database_path), settings=settings,
    )
    async with bind_tracker(tracker):
        out = await g.ainvoke(
            _state(settings, budget=1e-9),
            config={"configurable": {"thread_id": "bt"}},
        )
    assert out["status"] == "awaiting_review"
    assert out["escalation_reason"] == "budget"
    assert out["cost_so_far"] > 0
    assert llm.calls <= 2  # researcher plan ran once, maybe one more — then stopped


async def test_fund_resume_raises_budget_and_completes() -> None:
    settings = _settings()
    llm, search = _SpyLLM(), _SpySearch()
    g = _graph(settings, llm, search)
    cfg = {"configurable": {"thread_id": "bf"}}
    out = await g.ainvoke(_state(settings, budget=0.0), config=cfg)
    assert out["status"] == "awaiting_review"

    resumed = await g.ainvoke(
        Command(resume={"action": "fund", "additional_budget_usd": 0.50}),
        config=cfg,
    )
    assert resumed["status"] == "done"
    assert resumed["budget_usd"] == 0.50
    assert resumed["final_report"]
