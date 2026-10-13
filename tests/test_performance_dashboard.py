"""Performance dashboard surface — measured cost/latency aggregates.

Covers the Phase-11 contract: /dashboard/performance shape, the
human-vs-agent comparison (absent with zero completed jobs — never
fabricated), per-role and prompt-version mixes, the model price table,
and provider-reported token usage preferred over estimates.
"""

import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.cost_tracking import (
    MODEL_PRICES,
    UsageTracker,
    price_for,
    tracked_structured,
)
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.llm_client import StubLLM
from app.services.usage_store import UsageStore


@pytest.fixture()
def perf_client() -> Any:
    from app.graph.graph import build_graph
    from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm

    settings = Settings(_env_file=None)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = tempfile.mktemp(suffix=".sqlite")
    graph = build_graph(
        settings, llm=_stub_llm(), search=_FakeSearch(),
        scraper=_FakeScraper(), checkpointer=None,
    )
    app.state.runner = JobRunner(
        graph, JobStore(db), UsageStore(db), settings
    )
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    return app, db, settings


def test_performance_empty_state(perf_client: Any) -> None:
    app, _, _ = perf_client
    with TestClient(app) as c:
        r = c.get("/api/v1/dashboard/performance")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["jobs"] == 0
        assert body["success_rate"] is None
        # Empty state is explicit — never a fabricated comparison.
        assert body["human_vs_agent"] is None
        assert body["price_table_version"]


async def test_performance_populated(perf_client: Any) -> None:
    app, db, _ = perf_client
    jobs = JobStore(db)
    usage = UsageStore(db)
    from app.graph.state import Brief

    brief = Brief.model_validate(
        {"topic": "quarterly report", "report_type": "general",
         "budget_usd": 1.0}
    )
    job = await jobs.create("default", brief)
    await jobs.set_result(
        "default", job.job_id,
        {"report": "ok", "debate_rounds": 1},
        cost_usd=0.002, status="done",
    )
    await usage.record(
        job_id=job.job_id, workspace_id="default", role="writer",
        model="gpt-4o-mini", prompt_tokens=100, completion_tokens=50,
        cost_usd=0.002, prompt_name="writer", prompt_version=1,
    )

    with TestClient(app) as c:
        body = c.get("/api/v1/dashboard/performance").json()
        assert body["totals"]["by_status"]["done"] == 1
        assert body["success_rate"] == 1.0
        assert body["cost"]["by_role"]["writer"]["cost_usd"] == 0.002
        assert body["cost"]["by_role"]["writer"]["prompt_tokens"] == 100
        assert body["prompt_version_mix"][0]["prompt_name"] == "writer"
        assert body["budget"]["jobs_with_budget"] == 1
        assert body["budget"]["pct_used"] is not None
        human = body["human_vs_agent"]
        assert human["reports_completed"] == 1
        assert human["savings_usd"] > 0  # human rate ≫ agent cents
        assert body["daily"][0]["jobs"] == 1


# ─── price table ──────────────────────────────────────────────────────────────


def test_configured_models_have_prices() -> None:
    """Every model the deployment can select must be priced — a missing
    entry means costs silently fall back to defaults and lie."""
    s = Settings(
        _env_file=None,
        chat_model_name="gpt-4o-mini",
        judge_model="gpt-4o",
        reviewer_model="gpt-4.1-mini",
    )
    for model in (s.chat_model_name, s.judge_model, s.reviewer_model):
        assert model is not None
        assert price_for(model) is not None, f"unpriced model: {model}"
    # Dated variants resolve via prefix.
    assert price_for("gpt-4o-mini-2024-07-18") == MODEL_PRICES["gpt-4o-mini"]


def test_compute_cost_uses_model_table() -> None:
    settings = Settings(_env_file=None)
    usage = UsageStore(tempfile.mktemp(suffix=".sqlite"))
    tracker = UsageTracker(
        job_id="j", workspace_id="w", store=usage, settings=settings
    )
    cost = tracker.compute_cost(1_000_000, 1_000_000, model="gpt-4o")
    assert cost == 12.5  # 2.50 in + 10.00 out — table, not defaults
    # Unknown model → configured defaults (0.15/0.60).
    assert tracker.compute_cost(1_000_000, 1_000_000, model="other") == 0.75


# ─── provider-reported token usage ────────────────────────────────────────────


class _Out(BaseModel):
    ok: bool = True


class _UsageLLM(StubLLM):
    """Stub variant that reports provider-style token usage, like the
    OpenAI client does via usage_metadata."""

    async def structured(self, schema: Any, **kwargs: Any) -> Any:
        ctx = kwargs.get("context")
        if ctx is not None:
            ctx["token_usage"] = {"prompt_tokens": 1234,
                                  "completion_tokens": 77}
        return _Out()


async def test_provider_usage_preferred_over_estimate(
    perf_client: Any,
) -> None:
    from app.core.cost_tracking import bind_tracker

    _, db, settings = perf_client
    usage = UsageStore(db)
    tracker = UsageTracker(
        job_id="j", workspace_id="w", store=usage, settings=settings
    )
    ctx: dict[str, Any] = {}
    async with bind_tracker(tracker):
        await tracked_structured(
            _UsageLLM(), _Out, role="writer", model="gpt-4o-mini",
            system="s", user="u", context=ctx,
        )
    rec = tracker.records[0]
    # Provider numbers win over the chars/4 estimate — the estimate for
    # this call would be 1 and ~8, not 1234/77.
    assert rec.prompt_tokens == 1234
    assert rec.completion_tokens == 77
    assert "token_usage" not in ctx  # consumed, not left in context
