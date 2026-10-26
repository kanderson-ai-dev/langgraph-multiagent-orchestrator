"""Passage index: deterministic ids, ranked top-K, budget respected."""

from app.graph.passages import build_passage_index, render_passages
from app.graph.state import EvidenceItem

_S1 = (
    "Navigation menu and cookie text sit at the top of the page here. "
    "Mid-market SaaS vendors adopted RAG to cut hallucinations by 40 %. "
    "The rest of the paragraph adds more context and background detail. "
    "Pricing starts at ten dollars per seat for the standard tier plan. "
    "Additional sentences pad the source so several passages exist. "
    "A final unrelated remark about fonts and layouts closes the page."
)
_S2 = (
    "This vendor page opens with marketing copy and a hero slogan. "
    "SOC 2 Type II reports are available to customers under an NDA. "
    "The compliance section continues with audit cadence details. "
    "Case studies follow with customer quotes and rollout timelines."
)


def _ev() -> list[EvidenceItem]:
    return [
        EvidenceItem(evidence_id="a", url="https://a.test/x", text=_S1),
        EvidenceItem(evidence_id="b", url="https://b.test/y", text=_S2),
    ]


def test_ids_stable_and_resolve() -> None:
    i1 = build_passage_index(_ev(), "RAG adoption hallucinations")
    i2 = build_passage_index(_ev(), "RAG adoption hallucinations")
    assert [p.pid for p in i1.passages] == [p.pid for p in i2.passages]
    p = i1.passages[0]
    assert i1.resolve(p.pid) is p
    assert i1.resolve("S9-P9") is None
    # Passage text is a verbatim substring of its source.
    assert p.text in (_S1 if p.source_index == 0 else _S2)


def test_ranking_prefers_relevant_passages() -> None:
    idx = build_passage_index(_ev(), "RAG hallucination reduction in SaaS")
    texts = " ".join(p.text for p in idx.passages if p.source_index == 0)
    assert "40 %" in texts  # the relevant claim outranks the menu boilerplate


def test_budget_respected() -> None:
    idx = build_passage_index(_ev(), "anything", char_budget=400)
    assert sum(len(p.text) for p in idx.passages) <= 400


def test_render_groups_by_source() -> None:
    idx = build_passage_index(_ev(), "RAG")
    out = render_passages(idx)
    assert "[source 0] https://a.test/x" in out
    assert "[S0-P" in out
    assert build_passage_index([], "q").passages == []
    assert render_passages(build_passage_index([], "q")) == "(no evidence gathered)"
