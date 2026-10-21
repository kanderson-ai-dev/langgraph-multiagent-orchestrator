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

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        self.calls += 1
        return [SearchResult(title="t", url="https://src.test/a", snippet="s")]


class _SpyScraper:
    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
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


# ─── Phase 9 — pre-call worst-case estimate ───────────────────────────────────


def _tracker(settings: Settings, tmp_path: Any, budget: float | None) -> Any:
    from app.core.cost_tracking import UsageTracker
    from app.services.usage_store import UsageStore

    return UsageTracker(
        job_id="est", workspace_id="ws",
        store=UsageStore(str(tmp_path / "u.sqlite")),
        settings=settings, budget_usd=budget,
    )


def test_estimate_counts_tokens_retries_and_prices(tmp_path: Any) -> None:
    """Worst-case = (prompt×in + max_completion×out) × (1 + retries)."""
    from app.core.cost_tracking import estimate_call_cost

    settings = _settings(llm_max_tokens=1000)
    llm = _SpyLLM()
    llm.max_retries = 2  # priced-model path also verifies retry factor
    est = estimate_call_cost(
        llm, model="gpt-4o-mini",
        system="s " * 500, user="u " * 500, settings=settings,
    )
    in_p, out_p = 0.15, 0.60
    attempt = est.prompt_tokens / 1e6 * in_p + 1000 / 1e6 * out_p
    assert est.worst_case_usd == attempt * 3
    assert est.max_completion_tokens == 1000


def test_estimate_unknown_model_never_free(tmp_path: Any) -> None:
    """An unpriced model falls back to configured prices — never $0."""
    from app.core.cost_tracking import estimate_call_cost

    settings = _settings(llm_max_tokens=500)
    est = estimate_call_cost(
        _SpyLLM(), model="totally-unknown-model",
        system="sys", user="usr", settings=settings,
    )
    assert est.priced is False
    assert est.worst_case_usd > 0


async def test_precall_refusal_never_invokes_llm(tmp_path: Any) -> None:
    """A call whose worst case exceeds the remaining budget is refused
    BEFORE the provider is hit."""
    import pytest

    from app.core.cost_tracking import BudgetExceededError, bind_tracker

    settings = _settings(llm_max_tokens=4096)
    llm = _SpyLLM()
    tracker = _tracker(settings, tmp_path, budget=1e-9)
    async with bind_tracker(tracker):
        with pytest.raises(BudgetExceededError):
            from app.core.cost_tracking import tracked_structured

            await tracked_structured(
                llm, ResearchPlan, role="researcher",
                model="stub", system="s " * 200, user="u " * 200,
            )
    assert llm.calls == 0


async def test_uncapped_and_none_tracker_bypass_estimate(tmp_path: Any) -> None:
    """``None`` budget = no cap → no pre-call accounting at all."""
    from app.core.cost_tracking import bind_tracker, tracked_structured

    settings = _settings()
    llm = _SpyLLM()
    async with bind_tracker(_tracker(settings, tmp_path, budget=None)):
        out = await tracked_structured(
            llm, ResearchPlan, role="researcher",
            model="stub", system="s", user="u",
        )
    assert isinstance(out, ResearchPlan)
    assert llm.calls == 1


async def test_clamped_completion_fits_remaining_budget(tmp_path: Any) -> None:
    """When the full completion cap won't fit but a smaller one does, the
    call runs with a clamped ``max_completion_tokens`` instead of dying."""
    from app.core.cost_tracking import (
        MIN_VIABLE_COMPLETION_TOKENS,
        bind_tracker,
        tracked_structured,
    )

    settings = _settings(llm_max_tokens=4096)
    llm = _SpyLLM()
    ctx: dict[str, Any] = {}
    # Fits ~1-2k completion tokens at default prices, not the 4096 cap.
    tracker = _tracker(settings, tmp_path, budget=0.001)
    async with bind_tracker(tracker):
        out = await tracked_structured(
            llm, ResearchPlan, role="researcher",
            model="stub", system="s", user="u", context=ctx,
        )
    assert isinstance(out, ResearchPlan)
    assert MIN_VIABLE_COMPLETION_TOKENS <= ctx["max_completion_tokens"] < 4096


def test_estimate_never_underestimates_stub_cost(tmp_path: Any) -> None:
    """The estimate must be ≥ what the stub ends up reporting — the
    estimate is a ceiling, never an optimistic guess."""
    from app.core.cost_tracking import estimate_call_cost

    settings = _settings()
    tracker = _tracker(settings, tmp_path, budget=None)
    prompts = [
        ("short", "u"),
        ("s " * 5000, "u " * 3000),
        ("emoji 😀🎉🚀 " * 200, "ok"),
    ]
    for system, user in prompts:
        est = estimate_call_cost(
            _SpyLLM(), model="stub",
            system=system, user=user, settings=settings,
        )
        stub_prompt_tokens = (len(system) + len(user)) // 4
        actual = tracker.compute_cost(stub_prompt_tokens, 800, model="stub")
        assert est.worst_case_usd >= actual, (system[:20], est, actual)


async def test_refused_call_escalates_to_human_review(tmp_path: Any) -> None:
    """Graph-level: a worker whose call is refused produces the same
    ``awaiting_review``/``budget`` escalation as the between-call gate —
    never an exception, never silent overspend."""
    from app.core.cost_tracking import bind_tracker

    settings = _settings(database_path=str(tmp_path / "u.sqlite"))
    llm, search = _SpyLLM(), _SpySearch()
    g = _graph(settings, llm, search)
    tracker = _tracker(settings, tmp_path, budget=1e-9)
    async with bind_tracker(tracker):
        out = await g.ainvoke(
            _state(settings, budget=1e-9),
            config={"configurable": {"thread_id": "precall"}},
        )
    assert out["status"] == "awaiting_review"
    assert out["escalation_reason"] == "budget"
    assert llm.calls == 0


async def test_zero_budget_on_tracker_refuses_everything(tmp_path: Any) -> None:
    """budget=0 on the tracker = zero cap → every paid call refused."""
    import pytest

    from app.core.cost_tracking import (
        BudgetExceededError,
        bind_tracker,
        tracked_structured,
    )

    settings = _settings()
    async with bind_tracker(_tracker(settings, tmp_path, budget=0.0)):
        with pytest.raises(BudgetExceededError):
            await tracked_structured(
                _SpyLLM(), ResearchPlan, role="researcher",
                model="stub", system="s", user="u",
            )


async def test_refusal_flag_cleared_after_funded_resume(tmp_path: Any) -> None:
    """``budget_blocked`` must not persist through the pause — a funded
    resume would otherwise re-escalate on the stale flag forever."""
    from app.core.cost_tracking import bind_tracker

    settings = _settings(database_path=str(tmp_path / "u.sqlite"))
    llm, search = _SpyLLM(), _SpySearch()
    g = _graph(settings, llm, search)
    cfg = {"configurable": {"thread_id": "flag"}}
    async with bind_tracker(_tracker(settings, tmp_path, budget=1e-9)):
        out = await g.ainvoke(_state(settings, budget=1e-9), config=cfg)
    assert out["status"] == "awaiting_review"
    assert out["escalation_reason"] == "budget"
    # The refusal text is in the transcript; the flag itself is already
    # consumed by the supervisor so it can't re-trigger after resume.
    assert any(
        "refused" in m.content.lower() for m in out["transcript"]
    )

    async with bind_tracker(_tracker(settings, tmp_path, budget=1.0)):
        resumed = await g.ainvoke(
            Command(resume={"action": "fund", "additional_budget_usd": 1.0}),
            config=cfg,
        )
    assert resumed["status"] == "done"
    assert not resumed.get("budget_blocked")
