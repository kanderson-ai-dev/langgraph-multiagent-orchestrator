"""Worker agents: Researcher, Writer, Reviewer — all with stubs, no network."""

from typing import Any

import pytest
from pydantic import BaseModel

from app.graph.nodes.researcher import ResearcherWorker, ResearchPlan
from app.graph.nodes.reviewer import ReviewerWorker, ReviewOutput, verify_citations
from app.graph.nodes.writer import (
    DraftCitation,
    DraftOutput,
    DraftOutputPassages,
    PassageCitation,
    WriterWorker,
)
from app.graph.state import Brief, Citation, EvidenceItem, ReportDraft, ReviewVerdict
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult


@pytest.fixture()
def brief(brief_kwargs: dict[str, object]) -> Brief:
    return Brief.model_validate(brief_kwargs)


def _evidence() -> list[EvidenceItem]:
    return [
        EvidenceItem(
            evidence_id="e1",
            url="https://src.test/rag",
            title="RAG overview",
            text=(
                "RAG systems retrieve documents to ground generation in "
                "sources, which enterprises report improves answer accuracy "
                "and reduces unsupported claims substantially in production."
            ),
        )
    ]


class _FakeSearch:
    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://src.test/rag", snippet="s")]


class _FakeScraper:
    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
        html = (
            b"<html><title>RAG overview</title><body>RAG systems retrieve "
            b"documents to ground generation in sources.</body></html>"
        )
        return ScrapedPage(url=url, status_code=200, content_type="text/html", body=html)


def _stub_llm() -> StubLLM:
    stub = StubLLM()

    def plan(schema: type[BaseModel], ctx: dict[str, Any] | None) -> BaseModel:
        return ResearchPlan(
            sub_questions=["What drives RAG adoption?"],
            queries=["RAG adoption mid-market SaaS"],
        )

    stub.register("ResearchPlan", plan)
    return stub


# ─── Researcher ─────────────────────────────────────────────────────────────


async def test_researcher_gathers_evidence(brief: Brief, prompts: Any) -> None:
    w = ResearcherWorker(
        llm=_stub_llm(),
        search=_FakeSearch(),
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        prompts=prompts,
        max_queries=3,
    )
    out = await w.run({"brief": brief})
    assert out["sub_questions"] == ["What drives RAG adoption?"]
    assert len(out["evidence"]) == 1
    assert out["evidence"][0].url == "https://src.test/rag"
    assert out["transcript"][0].sender == "researcher"


# ─── Writer ──────────────────────────────────────────────────────────────────


async def test_writer_produces_versioned_draft(brief: Brief, prompts: Any) -> None:
    stub = StubLLM()
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="RAG Adoption",
            markdown="# RAG Adoption\nRAG grounds generation.",
            citations=[
                DraftCitation(
                    claim="RAG grounds generation",
                    quote="RAG systems retrieve documents",
                    source_url="https://src.test/rag",
                )
            ],
        ),
    )
    w = WriterWorker(llm=stub, prompts=prompts)
    state = {"brief": brief, "evidence": _evidence()}
    out = await w.run(state)
    draft: ReportDraft = out["drafts"][0]
    assert draft.version == 1
    assert draft.title == "RAG Adoption"
    assert len(draft.citations) == 1

    # Second run → v2
    state["drafts"] = [draft]
    out2 = await w.run(state)
    assert out2["drafts"][0].version == 2


# ─── Reviewer ────────────────────────────────────────────────────────────────


async def test_reviewer_verifies_citations(brief: Brief, prompts: Any) -> None:
    stub = StubLLM()
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.5,
            rubric={
                "structure": 4.5, "clarity": 4.5, "grounding": 4.5, "tone": 4.5
            },
            feedback=[],
        ),
    )
    w = ReviewerWorker(llm=stub, prompts=prompts)
    draft = ReportDraft(
        version=1,
        title="T",
        markdown="# T",
        citations=[
            Citation(
                claim="ok",
                quote="RAG systems retrieve documents",
                source_url="https://src.test/rag",
            ),
            Citation(
                claim="fabricated",
                quote="totally invented text not present",
                source_url="https://src.test/rag",
            ),
        ],
    )
    out = await w.run({"brief": brief, "evidence": _evidence(), "drafts": [draft]})
    verdict: ReviewVerdict = out["latest_verdict"]
    assert verdict.unsupported_citations == 1
    assert out["debate_round"] == 1
    # support rate 0.5 < 0.90 → forced revise regardless of LLM verdict
    assert verdict.verdict == "revise"
    assert any("citation" in f.lower() for f in verdict.feedback)


async def test_reviewer_no_drafts(prompts: Any) -> None:
    w = ReviewerWorker(llm=StubLLM(), prompts=prompts)
    out = await w.run({"brief": Brief(topic="x y z"), "evidence": [], "drafts": []})
    assert out["latest_verdict"].verdict == "revise"


def test_verify_citations_helper() -> None:
    draft = ReportDraft(
        version=1,
        title="t",
        markdown="m",
        citations=[
            Citation(claim="c", quote="ground generation in sources", source_url="https://src.test/rag"),
            Citation(claim="c2", quote="not in source at all", source_url="https://nowhere.test"),
        ],
    )
    kept, dropped, _h, _u = verify_citations(draft, _evidence())
    assert len(kept) == 1
    assert dropped == 1


async def test_writer_extractive_mode_resolves_passage_ids(
    brief: Brief, prompts: Any
) -> None:
    """Extractive mode: writer emits passage ids, code resolves verbatim text."""
    stub = StubLLM()
    stub.register(
        "DraftOutputPassages",
        lambda s, c: DraftOutputPassages(
            title="RAG Adoption",
            markdown="# RAG Adoption",
            citations=[
                PassageCitation(
                    claim="RAG systems retrieve documents", passage_id="S0-P0"
                ),
                PassageCitation(claim="bogus", passage_id="S9-P9"),
            ],
        ),
    )
    w = WriterWorker(llm=stub, prompts=prompts, citation_mode="extractive")
    state = {"brief": brief, "evidence": _evidence()}
    out = await w.run(state)
    draft: ReportDraft = out["drafts"][0]
    assert len(draft.citations) == 2
    good, bad = draft.citations
    assert good.quote  # resolved verbatim text
    assert good.quote in _evidence()[0].text
    assert good.source_url == _evidence()[0].url
    assert bad.source_url == "passage:S9-P9" and bad.quote == ""


async def test_writer_keeps_weak_claims_for_adjudication(
    brief: Brief, prompts: Any
) -> None:
    """A claim whose content words aren't in the quote is inference
    attached to a topical passage — it stays cited so the semantic claim
    adjudicator can flag it; lexical coverage never deletes citations."""
    stub = StubLLM()
    stub.register(
        "DraftOutputPassages",
        lambda s, c: DraftOutputPassages(
            title="RAG Adoption",
            markdown=(
                "# RAG Adoption\n"
                "RAG systems retrieve documents [S0-P0]. "
                "Enterprises should probably pilot governance tooling "
                "before scaling [S0-P0]."
            ),
            citations=[
                PassageCitation(
                    claim="RAG systems retrieve documents", passage_id="S0-P0"
                ),
                PassageCitation(
                    claim=(
                        "enterprises should pilot governance tooling before "
                        "scaling cautiously"
                    ),
                    passage_id="S0-P0",
                ),
            ],
        ),
    )
    w = WriterWorker(llm=stub, prompts=prompts, citation_mode="extractive")
    out = await w.run({"brief": brief, "evidence": _evidence()})
    draft: ReportDraft = out["drafts"][0]
    # Both entries survive for the claim judge; markers stay put.
    assert len(draft.citations) == 2
    assert draft.markdown.count("[S0-P0]") == 2


async def test_writer_remaps_claim_to_stating_passage(
    brief: Brief, prompts: Any
) -> None:
    """Right claim, wrong passage: a citation whose claim clearly matches a
    different passage gets re-pointed there and its marker rewritten."""
    ev = _evidence() + [
        EvidenceItem(
            evidence_id="e2",
            url="https://src.test/pricing",
            title="Pricing",
            text=(
                "Usage-based pricing charges customers for actual "
                "consumption rather than seats, which finance teams report "
                "improves cost alignment in production systems."
            ),
            query="",
            sha256="x",
            content_type="text/html",
        )
    ]
    stub = StubLLM()
    stub.register(
        "DraftOutputPassages",
        lambda s, c: DraftOutputPassages(
            title="T",
            markdown=(
                "# T\n"
                "RAG systems retrieve documents [S0-P0]. "
                "Usage-based pricing charges customers for actual "
                "consumption [S0-P0]."
            ),
            citations=[
                PassageCitation(
                    claim="RAG systems retrieve documents", passage_id="S0-P0"
                ),
                PassageCitation(
                    claim=(
                        "usage-based pricing charges customers for actual "
                        "consumption"
                    ),
                    passage_id="S0-P0",
                ),
            ],
        ),
    )
    w = WriterWorker(llm=stub, prompts=prompts, citation_mode="extractive")
    out = await w.run({"brief": brief, "evidence": ev})
    draft: ReportDraft = out["drafts"][0]
    assert len(draft.citations) == 2
    assert draft.citations[0].ref == "S0-P0"
    assert draft.citations[1].ref == "S1-P0"
    assert draft.citations[1].source_url == "https://src.test/pricing"
    assert draft.citations[1].quote == ev[1].text
    assert "[S1-P0]" in draft.markdown
    assert draft.markdown.count("[S0-P0]") == 1


async def test_writer_revision_prompt_shows_previous_draft(
    brief: Brief, prompts: Any
) -> None:
    """F6: round 2 receives the v1 markdown, not a blank page."""
    captured: dict[str, str] = {}

    class _Spy(StubLLM):
        async def structured(self, schema, *, system, user, context=None):  # type: ignore[no-untyped-def]
            captured["user"] = user
            return await super().structured(
                schema, system=system, user=user, context=context
            )

    stub = _Spy()
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(title="T2", markdown="# new", citations=[]),
    )
    w = WriterWorker(llm=stub, prompts=prompts)
    prev = ReportDraft(version=1, title="T", markdown="# THE OLD DRAFT XYZ", citations=[])
    verdict = ReviewVerdict(
        draft_version=1, verdict="revise", score=3.0,
        feedback=["fix citations"], unsupported_citations=1,
    )
    await w.run({
        "brief": brief, "evidence": _evidence(),
        "drafts": [prev], "latest_verdict": verdict, "debate_round": 1,
    })
    assert "THE OLD DRAFT XYZ" in captured["user"]
    assert "MINIMAL" in captured["user"]
    assert "fix citations" in captured["user"]


# ─── mechanical rubric dimensions ────────────────────────────────────────────


def test_structure_score_full_template() -> None:
    from app.graph.nodes.reviewer import _structure_score
    from app.graph.teams import SECTION_TEMPLATES
    from tests.conftest import ALL_SECTIONS_MARKDOWN

    draft = ReportDraft(version=1, title="t", markdown=ALL_SECTIONS_MARKDOWN)
    for tpl in SECTION_TEMPLATES.values():
        assert _structure_score(draft, tpl) == (5.0, [])


def test_structure_score_missing_section() -> None:
    from app.graph.nodes.reviewer import _structure_score
    from app.graph.teams import SECTION_TEMPLATES

    md = "## Executive Summary\nx.\n## Findings\ny.\n## Sources\nz."
    draft = ReportDraft(version=1, title="t", markdown=md)
    score, missing = _structure_score(draft, SECTION_TEMPLATES["general"])
    assert score <= 3.0
    assert missing  # names the absent required headings


def test_grounding_audit_all_markers_resolve() -> None:
    from app.graph.nodes.reviewer import _grounding_audit

    draft = ReportDraft(
        version=1, title="t",
        markdown="Fact one [S0-P1]. Fact two [S1-P2].",
        citations=[
            Citation(claim="a", quote="q", source_url="u1", ref="S0-P1"),
            Citation(claim="b", quote="q", source_url="u2", ref="S1-P2"),
        ],
    )
    score, note = _grounding_audit(draft, list(draft.citations))
    assert score == 5.0 and "resolve" in note


def test_grounding_audit_unresolved_marker_penalized() -> None:
    from app.graph.nodes.reviewer import _grounding_audit

    draft = ReportDraft(
        version=1, title="t",
        markdown="Fact [S0-P1] and another [S9-P9].",
        citations=[Citation(claim="a", quote="q", source_url="u", ref="S0-P1")],
    )
    score, note = _grounding_audit(draft, list(draft.citations))
    assert score < 5.0 and "S9-P9" in note


def test_grounding_audit_no_markers_caps() -> None:
    from app.graph.nodes.reviewer import _grounding_audit

    draft = ReportDraft(
        version=1, title="t", markdown="Factual text, no markers.",
        citations=[Citation(claim="a", quote="q", source_url="u")],
    )
    score, _ = _grounding_audit(draft, list(draft.citations))
    assert score == 3.0


def _verdict_stub(verdicts: list[str]) -> "StubLLM":
    from app.graph.claim_support import ClaimVerdicts

    stub = StubLLM()
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.5,
            rubric={
                "structure": 4.5, "clarity": 4.5, "grounding": 4.5, "tone": 4.5
            },
            feedback=[],
        ),
    )
    stub.register(
        "ClaimVerdicts",
        lambda s, c, verdicts=verdicts: ClaimVerdicts(verdicts=verdicts),
    )
    return stub


def _verified_draft(n: int) -> ReportDraft:
    """Draft whose citations all mechanically verify against _evidence()."""
    from tests.conftest import ALL_SECTIONS_MARKDOWN

    text = _evidence()[0].text
    cites = [
        Citation(
            claim=f"claim {i}", quote=text[i * 10 : i * 10 + 40].strip(),
            source_url="https://src.test/rag", ref=f"S0-P{i}",
        )
        for i in range(n)
    ]
    return ReportDraft(
        version=1, title="t",
        markdown=ALL_SECTIONS_MARKDOWN
        + "\n"
        + " ".join(f"Fact {i} [S0-P{i}]." for i in range(n)),
        citations=cites,
    )


async def test_reviewer_blocks_on_no_claim_verdict(
    brief: Brief, prompts: Any
) -> None:
    """A 'no' verdict (unrelated/contradicting quote) blocks approval even
    when the yes-share would otherwise pass the gate bar."""
    n = 8
    stub = _verdict_stub(["yes"] * (n - 1) + ["no"])
    w = ReviewerWorker(llm=stub, prompts=prompts)
    out = await w.run(
        {"brief": brief, "evidence": _evidence(), "drafts": [_verified_draft(n)]}
    )
    v: ReviewVerdict = out["latest_verdict"]
    assert v.verdict == "revise"
    assert v.weak_claims == 1
    assert any("BLOCKING" in f for f in v.feedback)


async def test_reviewer_tolerates_partly_within_gate_share(
    brief: Brief, prompts: Any
) -> None:
    """'partly' is thin support, not a wrong citation — tolerated while
    fully-supported citations are a solid majority (≥2/3 ship bar)."""
    n = 8
    stub = _verdict_stub(["yes"] * (n - 1) + ["partly"])
    w = ReviewerWorker(llm=stub, prompts=prompts)
    out = await w.run(
        {"brief": brief, "evidence": _evidence(), "drafts": [_verified_draft(n)]}
    )
    assert out["latest_verdict"].verdict == "approve"


async def test_reviewer_blocks_below_claim_rate(
    brief: Brief, prompts: Any
) -> None:
    """A yes-share below the 2/3 ship bar is revise even without 'no'
    verdicts — a draft where half its citations are weak is not shippable."""
    n = 4
    stub = _verdict_stub(["yes", "yes", "partly", "partly"])
    w = ReviewerWorker(llm=stub, prompts=prompts)
    out = await w.run(
        {"brief": brief, "evidence": _evidence(), "drafts": [_verified_draft(n)]}
    )
    assert out["latest_verdict"].verdict == "revise"


async def test_claim_support_verbatim_claim_skips_judge() -> None:
    """A verbatim-substring claim is 'yes' by definition — no judge call.
    Spaceless matching rescues quotes whose whitespace extraction ate."""
    from app.graph.claim_support import ClaimVerdicts, judge_claim_support

    quote = (
        "Usage-Based vs Seat-Based Pricing for APIs. Picking a billing axis "
        "is the single most consequential decision in your pricing model, "
        "and for an API-first product it usually comes down to usage-based."
    )
    cits = [
        Citation(
            claim=(
                "Picking a billing axis is the single most consequential "
                "decision in your pricing model"
            ),
            quote=quote,
            source_url="https://x.test",
        ),
        Citation(
            claim=(
                "The report recommends evaluating both models carefully "
                "before rolling out any pricing change across the base"
            ),
            quote=quote,
            source_url="https://x.test",
        ),
    ]
    stub = StubLLM()
    stub.register(
        "ClaimVerdicts", lambda s, c: ClaimVerdicts(verdicts=["no"])
    )
    verdicts, rate = await judge_claim_support(cits, judge_llm=stub)
    assert verdicts == ["yes", "no"]  # auto-yes merged with the judged item
    assert rate == 0.5


async def test_claim_support_all_verbatim_needs_no_judge() -> None:
    """All-verbatim citations adjudicate without any judge call at all."""
    from app.graph.claim_support import judge_claim_support

    quote = (
        "In 2023 a global logistics giant lostover200millionafter a cloud "
        "vulnerability exposed real time data from 300 warehouses worldwide."
    )
    cits = [
        Citation(
            claim=(
                "In 2023, a global logistics giant lost over $200 million "
                "after a cloud vulnerability exposed real-time data from "
                "300 warehouses"
            ),
            quote=quote,
            source_url="https://x.test",
        ),
    ]
    verdicts, rate = await judge_claim_support(cits, judge_llm=StubLLM())
    assert verdicts == ["yes"]
    assert rate == 1.0
