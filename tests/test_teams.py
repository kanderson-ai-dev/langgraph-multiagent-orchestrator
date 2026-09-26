"""Dynamic team composition — report_type picks the worker set, specialists
run once between research and writing, tenant context flows to prompts."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.graph.deciders import rule_based_decider
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.specialist import SpecialistAnalysis
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief
from app.graph.teams import SECTION_TEMPLATES, TEAMS, team_for
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult

_HTML = (
    b"<html><title>Src</title><body>Vendor revenue grew 40% YoY and SOC2 "
    b"type II certification is current.</body></html>"
)


class _FakeSearch:
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        return [SearchResult(title="t", url="https://src.test/a", snippet="s")]


class _FakeScraper:
    async def scrape(self, url: str) -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html", body=_HTML
        )


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


def _stub_llm() -> StubLLM:
    stub = StubLLM()
    stub.register(
        "ResearchPlan",
        lambda s, c: ResearchPlan(sub_questions=["q1"], queries=["vendor health"]),
    )
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="Vendor Report",
            markdown="# Report\nVendor revenue grew 40% YoY.",
            citations=[
                DraftCitation(
                    claim="Revenue growth",
                    quote="Vendor revenue grew 40% YoY",
                    source_url="https://src.test/a",
                )
            ],
        ),
    )
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.4,
            rubric={
                "structure": 4.4, "clarity": 4.4, "grounding": 4.4, "tone": 4.4
            },
            feedback=[],
        ),
    )
    stub.register(
        "SpecialistAnalysis",
        lambda s, c: SpecialistAnalysis(
            findings=["revenue +40% YoY"],
            flags=["none material"],
            recommendation="vendor is financially healthy",
        ),
    )
    return stub


# ─── team composition ────────────────────────────────────────────────────────


@pytest.mark.parametrize("report_type", list(TEAMS))
def test_team_for_includes_core_backbone(report_type: str) -> None:
    team = team_for(report_type)
    assert {"researcher", "writer", "reviewer"} <= set(team)
    assert team[0] == "researcher" and team[-1] == "reviewer"


def test_specialist_teams_differ() -> None:
    assert "financial_analyst" in team_for("vendor_assessment")
    assert "compliance_analyst" in team_for("vendor_assessment")
    assert "compliance_analyst" in team_for("technical_due_diligence")
    assert "financial_analyst" not in team_for("technical_due_diligence")
    assert team_for("general") == ["researcher", "writer", "reviewer"]


def test_every_report_type_has_a_template() -> None:
    for rt in TEAMS:
        assert SECTION_TEMPLATES[rt]


# ─── rule-based decider routes specialists ───────────────────────────────────


def test_rule_decider_routes_specialists_once() -> None:
    from app.framework.registry import WorkerRegistry
    from app.graph.nodes.specialist import SpecialistWorker
    from app.graph.state import AnalystNote, EvidenceItem

    reg = WorkerRegistry()
    reg.register(SpecialistWorker("financial_analyst", "d", "f", StubLLM()))
    state: dict[str, Any] = {
        "evidence": [EvidenceItem(evidence_id="e", url="u")],
        "analyst_notes": [],
        "drafts": [],
    }
    d = rule_based_decider(state, reg)
    assert d.next_worker == "financial_analyst"
    # Once the note exists, the specialist is not re-routed.
    state["analyst_notes"] = [AnalystNote(analyst="financial_analyst")]
    d = rule_based_decider(state, reg)
    assert d.next_worker != "financial_analyst"


# ─── end-to-end with a specialist team ───────────────────────────────────────


async def test_vendor_assessment_runs_specialists_end_to_end() -> None:
    settings = _settings()
    g = build_graph(
        settings, llm=_stub_llm(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )
    state = initial_state(
        job_id="t1", workspace_id="ws",
        brief=Brief(
            topic="Assess Acme Corp as an infrastructure vendor",
            report_type="vendor_assessment",
            tenant_context="Series B fintech evaluating KYC vendors",
        ),
        max_debate_rounds=2,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "t1"}})
    assert out["status"] == "done"
    analysts = {n.analyst for n in out["analyst_notes"]}
    assert analysts == {"financial_analyst", "compliance_analyst"}
    senders = [m.sender for m in out["transcript"]]
    assert "financial_analyst" in senders and "compliance_analyst" in senders


async def test_general_team_skips_specialists() -> None:
    settings = _settings()
    g = build_graph(
        settings, llm=_stub_llm(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )
    state = initial_state(
        job_id="t2", workspace_id="ws",
        brief=Brief(topic="generic report on batteries"),
        max_debate_rounds=2,
    )
    out = await g.ainvoke(state, config={"configurable": {"thread_id": "t2"}})
    assert out["status"] == "done"
    assert out["analyst_notes"] == []


async def test_dispatch_count_lives_in_state() -> None:
    """Dispatch budget is per-job (state), not per-supervisor instance."""
    settings = _settings()
    g = build_graph(
        settings, llm=_stub_llm(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
    )
    state = initial_state(
        job_id="t3", workspace_id="ws",
        brief=Brief(topic="battery recycling economics"), max_debate_rounds=1,
    )
    out = await g.ainvoke(state)
    assert out["dispatches"] >= 3  # researcher + writer + reviewer at minimum
