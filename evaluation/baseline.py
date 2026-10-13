"""Single-prompt baseline — the honest counterfactual.

One LLM call drafts the report straight from the brief and the *same*
evidence bundle the pipeline gathered: no supervisor routing, no research
sub-questions, no Writer↔Reviewer debate, no citation verification, no
input guardrail. This is the "just ask the model" alternative the
orchestration must beat to justify its complexity and cost.

Offline the draft comes from a deterministic stub variant that models
the classic single-shot failure mode — one grounded citation plus one
fabricated quote — so the side-by-side scorecard shows *what the
pipeline's verification step is for*. It is a simulation of the
mechanism, not a real-model quality measurement; live numbers come
from the credential-gated workflow.
"""

import time
from typing import Any

from app.core.config import Settings
from app.graph.citations import support_rate
from app.graph.nodes.reviewer import ReviewOutput, verify_citations
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief, Citation, EvidenceItem, ReportDraft
from app.services.llm_client import StructuredLLM, StubLLM
from app.services.prompt_eval import build_eval_stub

BASELINE_SYSTEM = (
    "You are a report writer. Given a business brief and an evidence bundle, "
    "produce a professional report draft in Markdown, citing sources with "
    "verbatim quotes where you use them."
)

_BASELINE_GRADER_USER = "Grade the following draft against the brief.\n\n{draft}"

_FABRICATED_QUOTE = "Market leaders universally agree adoption is inevitable"


def build_baseline_stub() -> StubLLM:
    """Deterministic single-shot draft — models the no-verification failure.

    Two citations: one verbatim from the fixture source, one fabricated.
    The baseline ships both because nothing verifies them.
    """
    stub = build_eval_stub()
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="Baseline Report",
            markdown=(
                "## Summary\nEnterprises adopting retrieval augmented "
                "generation report measurable grounding improvements, and "
                "market leaders universally agree adoption is inevitable."
            ),
            citations=[
                DraftCitation(
                    claim="Grounding improves with retrieval",
                    quote=(
                        "Enterprises adopting retrieval augmented generation "
                        "report measurable grounding improvements"
                    ),
                    source_url="https://eval.fixture/source",
                ),
                DraftCitation(
                    claim="Adoption is inevitable",
                    quote=_FABRICATED_QUOTE,
                    source_url="https://eval.fixture/source",
                ),
            ],
        ),
    )
    return stub


def _baseline_user(brief: Brief, evidence: list[EvidenceItem]) -> str:
    """Same evidence-bundle format the Writer sees — comparable input."""
    ev_block = "\n\n".join(
        f"[source {i}] {e.url}\n{e.text[:1500]}" for i, e in enumerate(evidence)
    ) or "(no evidence gathered)"
    reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"
    return (
        f"Topic: {brief.topic}\nAudience: {brief.audience}\n"
        f"Tone: {brief.tone}\nLanguage: {brief.language}\n"
        f"Requirements:\n{reqs}\n\n"
        f"Evidence (cite only from these sources):\n{ev_block}"
    )


async def run_baseline_case(
    *,
    case_id: str,
    brief: Brief,
    evidence: list[EvidenceItem],
    settings: Settings,
    review_system: str,
    llm: StructuredLLM | StubLLM | None = None,
) -> dict[str, Any]:
    """Draft in one LLM call; measure support/cost/latency — fix nothing.

    ``cost_usd`` covers the single generation call only; the grading call
    below is shared evaluator machinery, not part of the baseline system.
    Returns the same per-case metric schema as the pipeline.
    """
    llm = llm or build_baseline_stub()
    system = BASELINE_SYSTEM
    user = _baseline_user(brief, evidence)

    started = time.monotonic()
    out = await llm.structured(
        DraftOutput,
        system=system,
        user=user,
        context={"prompt_name": "baseline", "prompt_version": 0},
    )
    latency = time.monotonic() - started

    prompt_tokens = (len(system) + len(user)) // 4
    completion_tokens = len(str(out)) // 4
    cost = (
        prompt_tokens / 1_000_000 * settings.cost_input_price_per_1m
        + completion_tokens / 1_000_000 * settings.cost_output_price_per_1m
    )

    # Grade the draft with the same rubric the pipeline's Reviewer uses —
    # measurement only; the baseline never revises or verifies.
    review = await llm.structured(
        ReviewOutput,
        system=review_system,
        user=_BASELINE_GRADER_USER.format(draft=out.markdown),
        context={"prompt_name": "baseline-grader", "prompt_version": 0},
    )

    draft = ReportDraft(
        version=1,
        title=out.title,
        markdown=out.markdown,
        citations=[
            Citation(claim=c.claim, quote=c.quote, source_url=c.source_url)
            for c in out.citations
        ],
    )
    kept, _dropped, _h = verify_citations(draft, evidence)
    return {
        "case_id": case_id,
        "citation_support": support_rate(len(kept), len(draft.citations)),
        "rubric_score": review.score if isinstance(review, ReviewOutput) else 0.0,
        "converged": True,  # no debate to converge — by definition
        "blocked": False,  # no input guardrail — always produces a report
        "produced_report": True,
        "llm_calls": 1,
        "cost_usd": round(cost, 8),
        "duration_seconds": round(latency, 3),
    }


def aggregate_baseline(results: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate baseline metrics — mirrors ``aggregate_metrics`` shape.

    Scores are averaged over non-blocked cases only (same denominator as
    the pipeline); ``blocked_rate`` is the fraction of expected-blocked
    cases where the baseline *refused* to produce a report — it has no
    guardrail, so honestly this is 0.0.
    """
    normal = [r for r in results if not r["expected_blocked"]]
    blocked = [r for r in results if r["expected_blocked"]]
    base = [r["baseline"] for r in normal]
    base_blocked = [r["baseline"] for r in blocked]

    def _avg(key: str, rows: list[dict[str, Any]]) -> float:
        return sum(r[key] for r in rows) / len(rows) if rows else 0.0

    return {
        "citation_support_rate": _avg("citation_support", base),
        "rubric_score_avg": _avg("rubric_score", base),
        "blocked_rate": (
            sum(1 for r in base_blocked if not r["produced_report"])
            / len(base_blocked)
            if base_blocked
            else 1.0
        ),
        "avg_cost_usd": _avg("cost_usd", base),
        "avg_latency_seconds": _avg("duration_seconds", base),
        "avg_llm_calls": _avg("llm_calls", base),
    }
