"""EDD harness smoke tests — dataset shape and evaluator correctness."""


from app.graph.state import (
    Citation,
    EvidenceItem,
    OrchestrationState,
    ReportDraft,
    ReviewVerdict,
)
from evaluation.dataset import DATASET
from evaluation.evaluators import (
    blocked_correctly,
    citation_support,
    debate_converged,
    rubric_score,
)


def test_dataset_is_versioned_and_valid() -> None:
    ids = [c.case_id for c in DATASET]
    assert len(ids) == len(set(ids))
    assert any(c.expect_blocked for c in DATASET)
    assert any(not c.expect_blocked for c in DATASET)


def _state_with(verdict: str = "approve", support: bool = True) -> OrchestrationState:
    evidence = [EvidenceItem(
        evidence_id="e", url="https://s.test", title="t",
        text="the exact quote lives here in the source text",
    )]
    quote = "the exact quote lives here" if support else "fabricated quote"
    return OrchestrationState(
        status="done",
        evidence=evidence,
        drafts=[ReportDraft(
            version=1, title="t", markdown="m",
            citations=[Citation(claim="c", quote=quote, source_url="https://s.test")],
        )],
        debate_round=1,
        max_debate_rounds=3,
        latest_verdict=ReviewVerdict(
            draft_version=1, verdict=verdict, score=4.5  # type: ignore[arg-type]
        ),
    )


def test_citation_support_evaluator() -> None:
    assert citation_support(_state_with(support=True)) == 1.0
    assert citation_support(_state_with(support=False)) == 0.0


def test_rubric_and_convergence() -> None:
    assert rubric_score(_state_with()) == 4.5
    assert debate_converged(_state_with("approve"))
    assert not debate_converged(_state_with("revise"))


def test_blocked_correctly_evaluator() -> None:
    assert blocked_correctly(OrchestrationState(status="blocked"))
    assert not blocked_correctly(OrchestrationState(status="done"))
