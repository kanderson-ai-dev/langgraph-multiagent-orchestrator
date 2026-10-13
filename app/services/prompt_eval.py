"""Prompt evaluation service — offline EDD harness reused by API and CLI.

The deterministic stub-LLM + fixture-tools evaluation lives here so both
``evaluation/run_eval.py`` (the CI gate) and the prompt-lifecycle routes
(A/B tests, gated promotion) execute *the same* pipeline measurement —
a candidate prompt is judged by exactly the yardstick CI enforces.

Nothing in this module performs network calls: the search/scraper clients
are fixtures and the default LLM is the deterministic stub. Live callers
inject a real ``StructuredLLM`` explicitly.
"""

import asyncio
import tempfile
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel

from app.core.config import Settings
from app.core.cost_tracking import UsageTracker, bind_tracker
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput, Rubric
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief, OrchestrationState
from app.services.llm_client import StubLLM
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult
from app.services.usage_store import UsageStore

# Gated metrics — identical to the project objective / CI thresholds.
EDD_THRESHOLDS: dict[str, float] = {
    "citation_support_rate": 0.90,
    "rubric_score_avg": 4.0,
    "debate_convergence_rate": 0.85,
    "blocked_rate": 1.0,
}

# Metrics measured on a 1–5 scale tolerate a small promotion epsilon;
# safety metrics must never regress at all.
_SCORE_METRICS = {"rubric_score_avg"}


class EvalCaseLike(Protocol):
    """Structural type of a dataset case (``evaluation.dataset.EvalCase``)."""

    case_id: str
    topic: str
    audience: str
    tone: str
    requirements: list[str]
    expect_blocked: bool


_FIXTURE_HTML = (
    b"<html><title>Evidence</title><body>"
    b"Enterprises adopting retrieval augmented generation report measurable "
    b"grounding improvements and reduced hallucination rates in production."
    b"</body></html>"
)


class FixtureSearch:
    """Fixture-based search — deterministic, offline."""

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"Source for {query[:40]}",
                url="https://eval.fixture/source",
                snippet="fixture evidence",
            )
        ]


class FixtureScraper:
    async def scrape(self, url: str) -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html", body=_FIXTURE_HTML
        )


def build_eval_stub() -> StubLLM:
    """Deterministic outputs: grounded draft citing fixture text, approve."""
    stub = StubLLM()
    stub.register(
        "ResearchPlan",
        lambda s, c: ResearchPlan(sub_questions=["q"], queries=["eval query"]),
    )
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="Evaluation Report",
            markdown=(
                "## Summary\nEnterprises adopting retrieval augmented "
                "generation report measurable grounding improvements."
            ),
            citations=[
                DraftCitation(
                    claim="Grounding improves with retrieval",
                    quote=(
                        "Enterprises adopting retrieval augmented generation "
                        "report measurable grounding improvements"
                    ),
                    source_url="https://eval.fixture/source",
                )
            ],
        ),
    )
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.5,
            rubric=Rubric(structure=4.5, clarity=4.5, grounding=4.5, tone=4.5),
            feedback=[],
        ),
    )
    return stub


async def run_eval_case(
    *,
    case_id: str,
    brief: Brief,
    expect_blocked: bool,
    settings: Settings,
    registry: PromptRegistry,
    llm: Any = None,
) -> tuple[dict[str, Any], OrchestrationState]:
    """Run one dataset case through the real graph.

    Returns ``(metrics, state)`` — callers aggregate the metrics and may
    mine the state (evidence, prompt versions, final report).
    """
    from evaluation.evaluators import (
        blocked_correctly,
        citation_support,
        debate_converged,
        rubric_score,
    )

    graph = build_graph(
        settings,
        llm=llm or build_eval_stub(),
        search=FixtureSearch(),
        scraper=FixtureScraper(),  # type: ignore[arg-type]
        prompts=registry,
    )
    state = initial_state(
        job_id=f"eval-{case_id}",
        workspace_id="eval",
        brief=brief,
        max_debate_rounds=settings.max_debate_rounds,
    )
    started = time.monotonic()
    # Bind a real usage tracker (throwaway sqlite) so cost/tokens are
    # measured by the same code path production jobs use.
    with tempfile.TemporaryDirectory() as tmp:
        tracker = UsageTracker(
            job_id=f"eval-{case_id}",
            workspace_id="eval",
            store=UsageStore(str(Path(tmp) / "usage.sqlite")),
            settings=settings,
        )
        async with bind_tracker(tracker):
            out = cast(OrchestrationState, await graph.ainvoke(state))
    metrics: dict[str, Any] = {
        "case_id": case_id,
        "status": out.get("status"),
        "citation_support": citation_support(out),
        "rubric_score": rubric_score(out),
        "converged": debate_converged(out),
        "blocked": blocked_correctly(out),
        "expected_blocked": expect_blocked,
        "debate_rounds": out.get("debate_round", 0),
        "duration_seconds": round(time.monotonic() - started, 3),
        "cost_usd": round(tracker.total_cost, 8),
        "llm_calls": len(tracker.records),
    }
    return metrics, out


def brief_for_case(case: EvalCaseLike) -> Brief:
    return Brief(
        topic=case.topic,
        audience=case.audience,
        tone=case.tone,
        requirements=case.requirements,
    )


def aggregate_metrics(results: list[dict[str, Any]]) -> dict[str, float]:
    normal = [r for r in results if not r["expected_blocked"]]
    blocked = [r for r in results if r["expected_blocked"]]
    return {
        "citation_support_rate": (
            sum(r["citation_support"] for r in normal) / len(normal) if normal else 0.0
        ),
        "rubric_score_avg": (
            sum(r["rubric_score"] for r in normal) / len(normal) if normal else 0.0
        ),
        "debate_convergence_rate": (
            sum(1 for r in normal if r["converged"]) / len(normal) if normal else 0.0
        ),
        "blocked_rate": (
            sum(1 for r in blocked if r["blocked"]) / len(blocked) if blocked else 1.0
        ),
        # Informational (never gated): the cost/latency the gates buy.
        "avg_cost_usd": (
            sum(r["cost_usd"] for r in normal) / len(normal) if normal else 0.0
        ),
        "avg_latency_seconds": (
            sum(r["duration_seconds"] for r in normal) / len(normal)
            if normal
            else 0.0
        ),
        "avg_llm_calls": (
            sum(r["llm_calls"] for r in normal) / len(normal) if normal else 0.0
        ),
    }


async def run_prompt_eval(
    settings: Settings,
    registry: PromptRegistry,
    *,
    cases: list[EvalCaseLike],
    overrides: dict[str, int] | None = None,
    llm: Any = None,
) -> tuple[dict[str, float], list[dict[str, Any]], list[OrchestrationState]]:
    """Run all cases under the given prompt overrides.

    Returns ``(aggregate_metrics, case_results, case_states)``.
    """
    reg = registry.with_overrides(overrides) if overrides else registry
    results: list[dict[str, Any]] = []
    states: list[OrchestrationState] = []
    for case in cases:
        metrics, state = await run_eval_case(
            case_id=case.case_id,
            brief=brief_for_case(case),
            expect_blocked=case.expect_blocked,
            settings=settings,
            registry=reg,
            llm=llm,
        )
        results.append(metrics)
        states.append(state)
    return aggregate_metrics(results), results, states


class ABResult(BaseModel):
    """Side-by-side metric comparison: promoted vs candidate."""

    name: str
    promoted_version: int
    candidate_version: int
    promoted_metrics: dict[str, float]
    candidate_metrics: dict[str, float]
    deltas: dict[str, float]


async def run_ab_eval(
    settings: Settings,
    registry: PromptRegistry,
    name: str,
    candidate_version: int,
    *,
    cases: list[EvalCaseLike],
    llm: Any = None,
) -> ABResult:
    """A/B the promoted prompt against a candidate over the eval dataset."""
    promoted_version = registry.get(name).version
    promoted_metrics, _, _ = await run_prompt_eval(
        settings, registry, cases=cases,
        overrides={name: promoted_version}, llm=llm,
    )
    candidate_metrics, _, _ = await run_prompt_eval(
        settings, registry, cases=cases,
        overrides={name: candidate_version}, llm=llm,
    )
    deltas = {
        m: round(candidate_metrics[m] - promoted_metrics[m], 6)
        for m in promoted_metrics
    }
    return ABResult(
        name=name,
        promoted_version=promoted_version,
        candidate_version=candidate_version,
        promoted_metrics=promoted_metrics,
        candidate_metrics=candidate_metrics,
        deltas=deltas,
    )


def promotion_gate(
    ab: ABResult, *, epsilon: float
) -> list[str]:
    """Reasons a candidate must be blocked from promotion (empty = clean).

    A candidate fails when a gated metric is below its threshold *or* it
    regresses versus the promoted version by more than the epsilon allowed
    for that metric family (0.0 safety / ``epsilon`` for scores).
    """
    failures: list[str] = []
    for metric, threshold in EDD_THRESHOLDS.items():
        value = ab.candidate_metrics.get(metric, 0.0)
        base = ab.promoted_metrics.get(metric, 0.0)
        allowed_epsilon = epsilon if metric in _SCORE_METRICS else 0.0
        if value < threshold:
            failures.append(f"{metric}={value:.3f} below threshold {threshold}")
        elif base - value > allowed_epsilon:
            failures.append(
                f"{metric} regressed {base:.3f} -> {value:.3f}"
                f" (allowed epsilon {allowed_epsilon})"
            )
    return failures


_T = TypeVar("_T")


async def bounded_eval(coro: Awaitable[_T], *, timeout: float = 25.0) -> _T:
    """Bound an A/B eval — routes must never block on a runaway eval."""
    return await asyncio.wait_for(coro, timeout=timeout)
