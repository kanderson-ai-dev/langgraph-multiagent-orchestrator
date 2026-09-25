"""EDD evaluators — the metrics that gate CI.

- ``citation_support`` — fraction of a report's citations verifiably present
  in the fetched source text (verbatim/punctuation-insensitive/≥0.85 fuzzy).
- ``rubric_score`` — the Reviewer's 1–5 rubric average for the final draft.
- ``debate_convergence`` — whether the job reached an approved verdict within
  ``max_debate_rounds`` without human escalation.
- ``blocked_correctly`` — security cases must be blocked by the input guardrail.
"""

from app.graph.citations import support_rate
from app.graph.nodes.reviewer import verify_citations
from app.graph.state import OrchestrationState, ReportDraft


def citation_support(state: OrchestrationState) -> float:
    drafts: list[ReportDraft] = state.get("drafts", [])
    if not drafts:
        return 0.0
    kept, dropped = verify_citations(drafts[-1], state.get("evidence", []))
    return support_rate(len(kept), len(drafts[-1].citations))


def rubric_score(state: OrchestrationState) -> float:
    verdict = state.get("latest_verdict")
    return verdict.score if verdict else 0.0


def debate_converged(state: OrchestrationState) -> bool:
    """True when a draft was approved within the debate budget."""
    verdict = state.get("latest_verdict")
    return (
        verdict is not None
        and verdict.verdict == "approve"
        and state.get("debate_round", 0) <= state.get("max_debate_rounds", 0)
        and state.get("status") == "done"
    )


def blocked_correctly(state: OrchestrationState) -> bool:
    return state.get("status") == "blocked"
