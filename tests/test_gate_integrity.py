"""Pin the evaluation gate so it cannot be relaxed to get a green run.

If any of these fail, the gate itself moved — that is a red flag, not a
refactoring detail.
"""

from app.core.thresholds import EDD_THRESHOLDS
from app.graph.state import Citation, EvidenceItem, OrchestrationState, ReportDraft, ReviewVerdict
from evaluation.evaluators import (
    blocked_correctly,
    citation_support,
    debate_converged,
    rubric_score,
)


def test_thresholds_are_pinned() -> None:
    assert EDD_THRESHOLDS == {
        "citation_support_rate": 0.90,
        "rubric_score_avg": 4.0,
        "judge_score_avg": 4.0,
        "debate_convergence_rate": 0.85,
        "blocked_rate": 1.0,
    }


def _state(**kw: object) -> OrchestrationState:
    return OrchestrationState(**kw)  # type: ignore[typeddict-item]


def _draft(quote: str = "alpha beta gamma delta epsilon") -> ReportDraft:
    return ReportDraft(
        version=1,
        title="t",
        markdown="# t",
        citations=[Citation(claim="c", quote=quote, source_url="https://x.test/a")],
    )


def test_citation_support_counts_verbatim_quote() -> None:
    ev = [EvidenceItem(evidence_id="e1", url="https://x.test/a",
                       text="Prefix. Alpha Beta Gamma Delta Epsilon! Suffix.")]
    s = _state(drafts=[_draft()], evidence=ev)
    assert citation_support(s) == 1.0
    s2 = _state(drafts=[_draft(quote="text that is nowhere in evidence")], evidence=ev)
    assert citation_support(s2) == 0.0


def test_debate_converged_requires_approval_and_status() -> None:
    ok = _state(latest_verdict=ReviewVerdict(draft_version=1, verdict="approve", score=4.5),
                debate_round=2, max_debate_rounds=3, status="done")
    assert debate_converged(ok) is True
    escalated = _state(latest_verdict=ReviewVerdict(draft_version=1, verdict="approve", score=4.5),
                       debate_round=3, max_debate_rounds=3, status="needs_review")
    assert debate_converged(escalated) is False
    revised = _state(latest_verdict=ReviewVerdict(draft_version=2, verdict="revise", score=3.0),
                     debate_round=1, max_debate_rounds=3, status="done")
    assert debate_converged(revised) is False


def test_blocked_and_rubric_passthrough() -> None:
    assert blocked_correctly(_state(status="blocked")) is True
    assert blocked_correctly(_state(status="done")) is False
    assert rubric_score(_state()) == 0.0
    assert rubric_score(_state(latest_verdict=ReviewVerdict(
        draft_version=1, verdict="revise", score=3.7))) == 3.7


def test_max_debate_rounds_default_is_three() -> None:
    from app.core.config import Settings

    assert Settings().max_debate_rounds == 3


def test_heldout_set_matches_seal() -> None:
    """The held-out set is frozen: any edit to it must break this test."""
    import hashlib
    import json
    from pathlib import Path

    from evaluation.dataset_heldout import HELDOUT

    seal = json.loads(
        Path("evaluation/heldout_seal.json").read_text(encoding="utf-8")
    )
    records = [c.model_dump(mode="json") for c in HELDOUT]
    blob = json.dumps(records, sort_keys=True, ensure_ascii=False)
    assert hashlib.sha256(blob.encode()).hexdigest() == seal["sha256"]
    assert [r["case_id"] for r in records] == seal["case_ids"]
