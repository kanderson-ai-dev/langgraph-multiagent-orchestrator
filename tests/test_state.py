"""Domain model validation and the accumulate reducer."""

import pytest

from app.graph.state import (
    AgentMessage,
    Brief,
    Citation,
    ReportDraft,
    ReviewVerdict,
    accumulate,
)


def test_brief_minimal_valid(brief_kwargs: dict[str, object]) -> None:
    brief = Brief.model_validate(brief_kwargs)
    assert brief.language == "en"
    assert brief.requirements


def test_brief_rejects_too_short_topic(brief_kwargs: dict[str, object]) -> None:
    brief_kwargs["topic"] = "ab"
    with pytest.raises(ValueError):
        Brief.model_validate(brief_kwargs)


def test_draft_versions_and_citations() -> None:
    draft = ReportDraft(
        version=2,
        title="RAG adoption",
        markdown="# Report",
        citations=[Citation(claim="c", quote="q", source_url="https://x.test/a")],
    )
    assert draft.version == 2
    assert draft.citations[0].source_url.endswith("/a")


def test_verdict_bounds() -> None:
    with pytest.raises(ValueError):
        ReviewVerdict(draft_version=1, verdict="approve", score=6.0)
    v = ReviewVerdict(draft_version=1, verdict="revise", score=3.5, feedback=["citation gap"])
    assert v.verdict == "revise"


def test_agent_message_kinds() -> None:
    m = AgentMessage(sender="writer", recipient="reviewer", kind="result", content="draft v1")
    assert m.round == 0
    with pytest.raises(ValueError):
        AgentMessage(sender="x", recipient="y", kind="nonsense", content="c")  # type: ignore[arg-type]


def test_accumulate_reducer() -> None:
    assert accumulate(None, [1, 2]) == [1, 2]
    assert accumulate([1], [2, 3]) == [1, 2, 3]
    assert accumulate([], []) == []
