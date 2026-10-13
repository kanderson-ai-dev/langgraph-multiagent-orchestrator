"""Prompt provenance — every LLM call attributable to (name, version, sha256)."""

from typing import Any

import aiosqlite
from prometheus_client import REGISTRY

from app.core.config import Settings
from app.core.cost_tracking import UsageTracker, bind_tracker, tracked_structured
from app.graph.deciders import SupervisorRoute
from app.graph.graph import build_graph, initial_state
from app.graph.state import AgentMessage, Brief
from app.services.audit import audit_root, chain_transcript, verify_chain
from app.services.llm_client import StubLLM
from app.services.pdf_export import markdown_to_pdf
from app.services.prompt_registry import PromptRegistry
from app.services.usage_store import UsageStore
from tests.test_graph import (
    _FakeScraper,
    _FakeSearch,
    _settings,
    _stub_llm,
)

# ─── usage store columns + migration ─────────────────────────────────────────


async def test_usage_store_persists_prompt_provenance(tmp_path: Any) -> None:
    store = UsageStore(str(tmp_path / "u.sqlite"))
    ev = await store.record(
        job_id="j", workspace_id="ws", role="writer", model="m",
        prompt_name="writer", prompt_version=3, prompt_sha256="abc",
    )
    assert ev.prompt_name == "writer" and ev.prompt_version == 3
    async with aiosqlite.connect(str(tmp_path / "u.sqlite")) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM usage_events")
        ).fetchone()
    assert row["prompt_name"] == "writer"
    assert row["prompt_version"] == 3
    assert row["prompt_sha256"] == "abc"


async def test_usage_store_migrates_legacy_db(tmp_path: Any) -> None:
    """A database created before provenance columns existed gets ALTERed."""
    db_path = str(tmp_path / "legacy.sqlite")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE usage_events ("
            " event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,"
            " workspace_id TEXT NOT NULL, role TEXT NOT NULL,"
            " model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,"
            " completion_tokens INTEGER NOT NULL DEFAULT 0,"
            " cost_usd REAL NOT NULL DEFAULT 0.0,"
            " latency_ms REAL NOT NULL DEFAULT 0.0,"
            " created_at TEXT NOT NULL)"
        )
        await db.commit()
    store = UsageStore(db_path)
    ev = await store.record(
        job_id="j", workspace_id="ws", role="r", model="m",
        prompt_name="writer", prompt_version=1,
    )
    assert ev.prompt_version == 1


# ─── tracked_structured carries provenance to usage + metrics ────────────────


async def test_tracked_call_records_prompt_meta(
    tmp_path: Any, settings: Settings
) -> None:
    store = UsageStore(str(tmp_path / "u.sqlite"))
    tracker = UsageTracker(
        job_id="j", workspace_id="ws", store=store, settings=settings
    )
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int = 1

    before = REGISTRY.get_sample_value(
        "orchestrator_llm_calls_total",
        {"role": "writer", "prompt_version": "writer@v1"},
    ) or 0.0
    async with bind_tracker(tracker):
        await tracked_structured(
            StubLLM(), Out,
            role="writer", model="m", system="s", user="u",
            context={
                "prompt_name": "writer",
                "prompt_version": 1,
                "prompt_sha256": "deadbeef",
            },
        )
    rec = tracker.records[0]
    assert (rec.prompt_name, rec.prompt_version, rec.prompt_sha256) == (
        "writer", 1, "deadbeef",
    )
    after = REGISTRY.get_sample_value(
        "orchestrator_llm_calls_total",
        {"role": "writer", "prompt_version": "writer@v1"},
    )
    assert after == before + 1


# ─── graph state provenance ──────────────────────────────────────────────────


async def test_finished_job_records_all_worker_prompt_versions(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    settings = _settings()
    g = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        prompts=prompts,
    )
    state = initial_state(
        job_id="pv1", workspace_id="ws",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=3,
    )
    out = await g.ainvoke(state)
    versions = out["prompt_versions"]
    for role in ("researcher", "writer", "reviewer"):
        assert versions[role] == 1
    # Worker completion events carry their prompt version into the transcript.
    result_msgs = [
        m for m in out["transcript"]
        if m.kind == "result" and m.sender in ("researcher", "writer")
    ]
    assert result_msgs and all(m.prompt_version == 1 for m in result_msgs)


class _FinishLLM:
    """Non-stub LLM that routes the supervisor straight to FINISH."""

    async def structured(
        self, schema: type[Any], **kw: Any
    ) -> Any:
        return SupervisorRoute(next_worker="FINISH", reason="done")


async def test_llm_decider_records_supervisor_version(
    brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    settings = _settings()
    g = build_graph(
        settings, llm=_FinishLLM(),  # type: ignore[arg-type]
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        prompts=prompts,
    )
    state = initial_state(
        job_id="pv2", workspace_id="ws",
        brief=Brief.model_validate(brief_kwargs),
        max_debate_rounds=3,
    )
    out = await g.ainvoke(state)
    assert out["prompt_versions"]["supervisor"] == 1


async def test_specialist_prompt_version_recorded(
    prompts: PromptRegistry
) -> None:
    settings = _settings()
    g = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        prompts=prompts,
    )
    state = initial_state(
        job_id="pv3", workspace_id="ws",
        brief=Brief(topic="vendor check", report_type="vendor_assessment"),
        max_debate_rounds=2,
    )
    out = await g.ainvoke(state)
    versions = out["prompt_versions"]
    assert versions["financial_analyst"] == 1
    assert versions["compliance_analyst"] == 1


# ─── audit chain provenance ──────────────────────────────────────────────────


def test_audit_entries_carry_prompt_version() -> None:
    msgs = [
        AgentMessage(
            sender="writer", recipient="reviewer", kind="result",
            content="drafted v1", prompt_version=2,
        )
    ]
    entries = chain_transcript(msgs)
    assert entries[0].prompt_version == 2


def test_tampering_prompt_version_invalidates_root() -> None:
    msgs = [
        AgentMessage(
            sender="writer", recipient="reviewer", kind="result",
            content="drafted v1", prompt_version=2,
        )
    ]
    root = audit_root(msgs)
    assert verify_chain(msgs, root)
    forged = [msgs[0].model_copy(update={"prompt_version": 99})]
    assert not verify_chain(forged, root)


# ─── runner result + PDF footer + metrics ────────────────────────────────────


async def test_job_result_contains_prompt_versions(
    tmp_path: Any, brief_kwargs: dict[str, object], prompts: PromptRegistry
) -> None:
    """End-to-end through JobRunner: result_json exposes the provenance map."""
    from langgraph.checkpoint.memory import MemorySaver

    from app.services.job_runner import JobRunner
    from app.services.job_store import JobStore

    settings = _settings()
    graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(), prompts=prompts,
    )
    db = str(tmp_path / "jobs.sqlite")
    runner = JobRunner(
        graph, JobStore(db), UsageStore(db), settings, prompt_registry=prompts
    )
    brief = Brief.model_validate(brief_kwargs)
    job = await runner.job_store.create("ws", brief)
    await runner.submit(job.job_id, "ws", brief)
    # Wait for the terminal event.
    async for e in runner.stream(job.job_id):
        if e.get("terminal"):
            break
    record = await runner.job_store.get("ws", job.job_id)
    assert record is not None and record.result is not None
    versions = record.result["prompt_versions"]
    assert versions["writer"] == 1 and versions["reviewer"] == 1


def test_pdf_footer_renders_without_error() -> None:
    pdf = markdown_to_pdf(
        "# Report\nBody.", title="t",
        footer="Prompt versions: writer v1 · reviewer v1",
    )
    assert pdf[:5] == b"%PDF-" and len(pdf) > 500


def test_registry_publish_metrics_sets_gauges(prompts: PromptRegistry) -> None:
    prompts.publish_metrics()
    assert (
        REGISTRY.get_sample_value(
            "orchestrator_prompt_version_info",
            {"name": "writer", "version": "1"},
        )
        == 1
    )
