"""Worker agents: Researcher, Writer, Reviewer — all with stubs, no network."""

from typing import Any

import pytest
from pydantic import BaseModel

from app.graph.nodes.researcher import ResearcherWorker, ResearchPlan
from app.graph.nodes.reviewer import ReviewerWorker, ReviewOutput, verify_citations
from app.graph.nodes.writer import DraftCitation, DraftOutput, WriterWorker
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
            text="RAG systems retrieve documents to ground generation in sources.",
        )
    ]


class _FakeSearch:
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://src.test/rag", snippet="s")]


class _FakeScraper:
    async def scrape(self, url: str) -> ScrapedPage:
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


async def test_researcher_gathers_evidence(brief: Brief) -> None:
    w = ResearcherWorker(
        llm=_stub_llm(),
        search=_FakeSearch(),
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        max_queries=3,
    )
    out = await w.run({"brief": brief})
    assert out["sub_questions"] == ["What drives RAG adoption?"]
    assert len(out["evidence"]) == 1
    assert out["evidence"][0].url == "https://src.test/rag"
    assert out["transcript"][0].sender == "researcher"


# ─── Writer ──────────────────────────────────────────────────────────────────


async def test_writer_produces_versioned_draft(brief: Brief) -> None:
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
    w = WriterWorker(llm=stub)
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


async def test_reviewer_verifies_citations(brief: Brief) -> None:
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
    w = ReviewerWorker(llm=stub)
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


async def test_reviewer_no_drafts() -> None:
    w = ReviewerWorker(llm=StubLLM())
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
    kept, dropped = verify_citations(draft, _evidence())
    assert len(kept) == 1
    assert dropped == 1
