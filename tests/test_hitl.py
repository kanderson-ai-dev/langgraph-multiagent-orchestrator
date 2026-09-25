"""HITL interrupt/resume via LangGraph interrupt() + Command(resume=...)."""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.graph.state import Brief
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


def _graph(settings: Settings, verdict: str = "revise") -> Any:
    return build_graph(
        settings,
        llm=_stub_llm(verdict=verdict),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )


async def _run_until_interrupt(
    settings: Settings, brief_kwargs: dict[str, object], job_id: str
) -> tuple[Any, dict[str, Any]]:
    g = _graph(settings)
    state = initial_state(
        job_id=job_id, workspace_id="ws-a",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=settings.max_debate_rounds,
    )
    cfg = {"configurable": {"thread_id": job_id}}
    out = await g.ainvoke(state, config=cfg)
    return g, out


async def test_interrupt_pauses_awaiting_review(
    brief_kwargs: dict[str, object]
) -> None:
    settings = _settings(max_debate_rounds=1)
    g, out = await _run_until_interrupt(settings, brief_kwargs, "hitl-1")
    # interrupt() leaves __interrupt__ in the result when paused.
    assert "__interrupt__" in out
    assert out["status"] == "awaiting_review"
    interrupt_payload = out["__interrupt__"][0].value
    assert interrupt_payload["reason"] == "debate_rounds_exhausted"
    assert set(interrupt_payload["actions"]) == {"approve", "edit", "reject", "fund"}


async def test_resume_approve_assembles(brief_kwargs: dict[str, object]) -> None:
    settings = _settings(max_debate_rounds=1)
    g, out = await _run_until_interrupt(settings, brief_kwargs, "hitl-2")
    cfg = {"configurable": {"thread_id": "hitl-2"}}
    resumed = await g.ainvoke(Command(resume={"action": "approve"}), config=cfg)
    assert resumed["status"] == "done"
    assert resumed["final_report"]


async def test_resume_edit_routes_back_to_writer(
    brief_kwargs: dict[str, object]
) -> None:
    settings = _settings(max_debate_rounds=1)
    g, out = await _run_until_interrupt(settings, brief_kwargs, "hitl-3")
    drafts_before = len(out["drafts"])
    cfg = {"configurable": {"thread_id": "hitl-3"}}
    resumed = await g.ainvoke(
        Command(resume={"action": "edit", "feedback": "tighten exec summary"}),
        config=cfg,
    )
    # A new revision happened post-resume (and then the loop re-escalates).
    assert len(resumed["drafts"]) > drafts_before
    verdicts = [m for m in resumed["transcript"] if "Human decision" in m.content]
    assert verdicts


async def test_resume_reject_terminates_failed(
    brief_kwargs: dict[str, object]
) -> None:
    settings = _settings(max_debate_rounds=1)
    g, out = await _run_until_interrupt(settings, brief_kwargs, "hitl-4")
    cfg = {"configurable": {"thread_id": "hitl-4"}}
    resumed = await g.ainvoke(Command(resume={"action": "reject"}), config=cfg)
    assert resumed["status"] == "failed"
    assert any("reject" in e.lower() for e in resumed["errors"])
