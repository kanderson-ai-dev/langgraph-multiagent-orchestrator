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

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.cost_tracking import UsageTracker, bind_tracker

# Gated metrics — imported from app.core.thresholds so the runtime
# (Reviewer's citation check) and the eval gate share one source of truth.
from app.core.thresholds import EDD_THRESHOLDS  # noqa: F401  (re-export)
from app.graph.claim_support import judge_claim_support
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput, Rubric
from app.graph.nodes.writer import (
    DraftCitation,
    DraftOutput,
    DraftOutputPassages,
    PassageCitation,
)
from app.graph.state import Brief, OrchestrationState
from app.graph.teams import SECTION_TEMPLATES
from app.services.llm_client import StubLLM, build_judge_llm
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult
from app.services.tracing import bind_trace
from app.services.usage_store import UsageStore

# Metrics measured on a 1–5 scale tolerate a small promotion epsilon;
# safety metrics must never regress at all.
_SCORE_METRICS = {"rubric_score_avg", "judge_score_avg"}


class EvalCaseLike(Protocol):
    """Structural type of a dataset case (``evaluation.dataset.EvalCase``)."""

    case_id: str
    topic: str
    audience: str
    tone: str
    requirements: list[str]
    expect_blocked: bool
    report_type: str = "general"
    language: str = "en"
    tenant_context: str = ""


_FIXTURE_HTML = (
    b"<html><title>Evidence</title><body>"
    b"Enterprises adopting retrieval augmented generation report measurable "
    b"grounding improvements and reduced hallucination rates in production."
    b"</body></html>"
)


class FixtureSearch:
    """Fixture-based search — deterministic, offline."""

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"Source for {query[:40]}",
                url="https://eval.fixture/source",
                snippet="fixture evidence",
            )
        ]


class FixtureScraper:
    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html", body=_FIXTURE_HTML
        )


class JudgeOutput(BaseModel):
    """LLM-as-judge verdict on the *final report* (not the process)."""

    score: float = Field(ge=1.0, le=5.0)
    rubric: Rubric
    justification: str = ""


def _judge_builder(schema: type[BaseModel], ctx: Any) -> BaseModel:
    """Deterministic offline judge — calibrated mid-high, not a perfect 5."""
    return JudgeOutput(
        score=4.5,
        rubric=Rubric(structure=4.5, clarity=4.5, grounding=4.5, tone=4.5),
        justification="offline stub judge",
    )


async def judge_report(
    brief: Brief,
    report: str,
    *,
    judge_system: str,
    judge_llm: Any,
    judge_prompt: Any = None,
) -> float:
    """Grade a finished report with the judge model; returns its 1–5 score.

    The call goes through ``tracked_structured`` so it records usage under
    role ``judge`` whenever a tracker is bound (live runs). ``run_eval_case``
    deliberately calls this *outside* the job tracker binding — like the
    baseline's grader, the judge is evaluator machinery, not pipeline spend.
    """
    from app.core.cost_tracking import tracked_structured

    context: dict[str, Any] = {"prompt_name": "judge"}
    if judge_prompt is not None:
        context.update(
            prompt_version=judge_prompt.version,
            prompt_sha256=judge_prompt.sha256,
        )
    out = await tracked_structured(
        judge_llm,
        JudgeOutput,
        role="judge",
        system=judge_system,
        user=(
            f"Brief — topic: {brief.topic}; audience: {brief.audience}; "
            f"tone: {brief.tone}; requirements: {brief.requirements}\n\n"
            f"Final report:\n{report}"
        ),
        context=context,
    )
    return float(out.score)


def _template_cover_markdown() -> str:
    """Markdown containing every SECTION_TEMPLATES heading so the mechanical
    structure check passes for ANY report_type: the check matches required
    headings as an ordered subsequence, so emitting each template's unique
    middle sections in order (with Recommendation/Sources last, where every
    template puts them) covers them all."""
    body: list[str] = []
    tail: list[str] = []
    for tpl in SECTION_TEMPLATES.values():
        for s in tpl.split(" · "):
            s = s.strip()
            if s in ("Recommendation", "Sources"):
                if s not in tail:
                    tail.append(s)
            elif s not in body:
                body.append(s)
    return "\n".join(f"## {s}\nStub paragraph." for s in (*body, *tail))


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
                _template_cover_markdown()
                + "\nEnterprises adopting retrieval augmented "
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
        "DraftOutputPassages",
        lambda s, c: DraftOutputPassages(
            title="Evaluation Report",
            markdown=(
                _template_cover_markdown()
                + "\nEnterprises adopting retrieval augmented "
                "generation report measurable grounding improvements."
            ),
            citations=[
                PassageCitation(
                    claim="Grounding improves with retrieval",
                    passage_id="S0-P0",
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


_AUTO_FUND_USD = 0.10
_MAX_AUTO_RESUMES = 6


def _auto_review_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """Operator stand-in for live evals — resolve a pause so the job
    delivers. Mirrors what the console offers: approve an existing draft,
    fund a budget stop so paid work continues, or send guidance back to
    the writer when there is nothing to approve yet."""
    allowed = set(payload.get("allowed_actions") or [])
    if "approve" in allowed:
        return {"action": "approve"}
    if "fund" in allowed:
        return {"action": "fund", "additional_budget_usd": _AUTO_FUND_USD}
    if "edit" in allowed:
        return {
            "action": "edit",
            "feedback": (
                "Address every open feedback item: keep the required "
                "section structure and cite only from gathered evidence."
            ),
        }
    return {"action": "reject", "feedback": "auto-review: no viable action"}


async def _invoke_resolving_hitl(
    graph: Any, state: dict[str, Any], callbacks: Any, job_id: str
) -> tuple[OrchestrationState, int]:
    """Invoke the graph with a checkpointer and resolve every ``interrupt()``
    like an on-call operator would — the live experiment measures the
    *delivered* artifact, not a job abandoned at the first pause."""
    from langgraph.types import Command

    config = {"configurable": {"thread_id": job_id}, "callbacks": callbacks}
    out = cast(OrchestrationState, await graph.ainvoke(state, config=config))
    resumes = 0
    while resumes < _MAX_AUTO_RESUMES:
        pending = cast(
            "list[Any] | None",
            out.get("__interrupt__") if isinstance(out, dict) else None,
        )
        if not pending:
            break
        payload = getattr(pending[0], "value", {})
        decision = _auto_review_decision(
            payload if isinstance(payload, dict) else {}
        )
        out = cast(
            OrchestrationState,
            await graph.ainvoke(Command(resume=decision), config=config),
        )
        resumes += 1
    return out, resumes


async def run_eval_case(
    *,
    case_id: str,
    brief: Brief,
    expect_blocked: bool,
    settings: Settings,
    registry: PromptRegistry,
    llm: Any = None,
    judge_llm: Any = None,
    search: Any = None,
    scraper: Any = None,
    llm_overrides: dict[str, Any] | None = None,
    auto_review: bool = False,
) -> tuple[dict[str, Any], OrchestrationState]:
    """Run one dataset case through the real graph.

    Returns ``(metrics, state)`` — callers aggregate the metrics and may
    mine the state (evidence, prompt versions, final report). Offline by
    default; live callers inject a real ``llm`` plus real ``search`` /
    ``scraper`` clients. ``auto_review=True`` attaches a checkpointer and
    resolves HITL interrupts automatically (approve/fund/edit) so a live
    run measures the *delivered* report, not a job left paused.
    """
    from evaluation.evaluators import (
        blocked_correctly,
        citation_support,
        debate_converged,
        rubric_score,
    )

    job_id = f"eval-{case_id}"
    state = initial_state(
        job_id=job_id,
        workspace_id="eval",
        brief=brief,
        max_debate_rounds=settings.max_debate_rounds,
    )
    started = time.monotonic()
    hitl_resumes = 0
    # Bind a real usage tracker (throwaway sqlite) so cost/tokens are
    # measured by the same code path production jobs use.
    with tempfile.TemporaryDirectory() as tmp:
        tracker = UsageTracker(
            job_id=job_id,
            workspace_id="eval",
            store=UsageStore(str(Path(tmp) / "usage.sqlite")),
            settings=settings,
        )
        async with bind_tracker(tracker):
            with bind_trace(
                name=f"eval:{case_id}",
                session_id=job_id,
                metadata={"case_id": case_id, "kind": "edd-eval"},
                tags=["eval"],
            ) as callbacks:
                if auto_review:
                    from langgraph.checkpoint.sqlite.aio import (
                        AsyncSqliteSaver,
                    )

                    async with AsyncSqliteSaver.from_conn_string(
                        str(Path(tmp) / "checkpoints.sqlite")
                    ) as checkpointer:
                        graph = build_graph(
                            settings,
                            llm=llm or build_eval_stub(),
                            search=search or FixtureSearch(),
                            scraper=scraper or FixtureScraper(),  # type: ignore[arg-type]
                            prompts=registry,
                            checkpointer=checkpointer,
                            llm_overrides=llm_overrides,
                        )
                        out, hitl_resumes = await _invoke_resolving_hitl(
                            graph, state, callbacks, job_id
                        )
                else:
                    graph = build_graph(
                        settings,
                        llm=llm or build_eval_stub(),
                        search=search or FixtureSearch(),
                        scraper=scraper or FixtureScraper(),  # type: ignore[arg-type]
                        prompts=registry,
                        llm_overrides=llm_overrides,
                    )
                    out = cast(
                        OrchestrationState,
                        await graph.ainvoke(
                            state, config={"callbacks": callbacks}
                        ),
                    )

    # LLM-as-judge on the finished report — a distinct model when
    # credentials allow; a deterministic stub builder offline. Its cost is
    # tracked under a *separate* throwaway tracker so the scorecard can
    # report judge spend honestly without mixing it into pipeline spend.
    judge_llm = judge_llm or build_judge_llm(settings)
    if isinstance(judge_llm, StubLLM):
        judge_llm.register_default("JudgeOutput", _judge_builder)
    report = out.get("final_report")
    judge_prompt = registry.render("judge")
    with tempfile.TemporaryDirectory() as jtmp:
        judge_tracker = UsageTracker(
            job_id=job_id,
            workspace_id="eval",
            store=UsageStore(str(Path(jtmp) / "judge.sqlite")),
            settings=settings,
        )
        async with bind_tracker(judge_tracker):
            judge_score = (
                await judge_report(
                    brief, report,
                    judge_system=judge_prompt.text,
                    judge_llm=judge_llm,
                    judge_prompt=judge_prompt,
                )
                if report
                else 0.0
            )
    metrics: dict[str, Any] = {
        "case_id": case_id,
        "status": out.get("status"),
        "citation_support": citation_support(out),
        "rubric_score": rubric_score(out),
        "judge_score": judge_score,
        "converged": debate_converged(out),
        "blocked": blocked_correctly(out),
        "expected_blocked": expect_blocked,
        "hitl_resumes": hitl_resumes,
        "debate_rounds": out.get("debate_round", 0),
        "duration_seconds": round(time.monotonic() - started, 3),
        "cost_usd": round(tracker.total_cost, 8),
        "judge_cost_usd": round(judge_tracker.total_cost, 8),
        "llm_calls": len(tracker.records),
    }
    # Integrity metric (D3): does each surviving quote actually back its
    # claim? Mirrors scripts/live_gate.py — post-hoc adjudication of the
    # last draft's citations by the judge model; a documented no-op on
    # the stub (empty verdicts), which the gate treats as "not scored".
    drafts = out.get("drafts") or []
    citations_final = drafts[-1].citations if drafts else []
    claim_verdicts, cs_rate = await judge_claim_support(
        list(citations_final), judge_llm=judge_llm
    )
    metrics["claim_support_rate"] = cs_rate
    metrics["claim_verdicts"] = claim_verdicts
    return metrics, out


def brief_for_case(case: EvalCaseLike) -> Brief:
    return Brief(
        topic=case.topic,
        audience=case.audience,
        tone=case.tone,
        requirements=case.requirements,
        report_type=case.report_type,  # type: ignore[arg-type]
        language=case.language,
        tenant_context=case.tenant_context,
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
        "judge_score_avg": (
            sum(r["judge_score"] for r in normal) / len(normal) if normal else 0.0
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
        # Judge spend is evaluator machinery, not pipeline spend — reported
        # separately so neither number can be misread as the other.
        "judge_cost_usd": (
            sum(r["judge_cost_usd"] for r in normal) / len(normal)
            if normal
            else 0.0
        ),
        "total_cost_usd_incl_judge": (
            sum(r["cost_usd"] + r["judge_cost_usd"] for r in normal)
            / len(normal)
            if normal
            else 0.0
        ),
        # Averaged over cases that produced verdicts — mirrors the live
        # gate's aggregate so both scorecards carry the same schema.
        "claim_support_rate_avg": (
            (
                lambda rates: sum(rates) / len(rates) if rates else 0.0
            )(
                [
                    float(r["claim_support_rate"])
                    for r in normal
                    if r.get("claim_verdicts")
                ]
            )
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
        if metric not in ab.candidate_metrics and metric not in (
            ab.promoted_metrics
        ):
            # A metric the eval pipeline never produced (e.g. claim
            # support, which needs a live judge model) is "not measured",
            # not zero — the live gate enforces it where it is measured.
            continue
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
