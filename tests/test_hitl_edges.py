"""HITL edge cases: guided actions, double-resume, restart, expiry, reject."""

from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings, get_settings
from app.graph.graph import build_graph
from app.graph.hitl import allowed_actions, approve_hint
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


def test_allowed_actions_matrix() -> None:
    """The legal review actions are a pure function of reason × has_draft."""
    assert allowed_actions("debate_rounds", True) == ["approve", "edit", "reject"]
    assert allowed_actions("debate_rounds", False) == ["edit", "reject"]
    assert allowed_actions("budget", True) == [
        "approve", "edit", "fund", "fund_and_continue", "reject",
    ]
    assert allowed_actions("budget", False) == [
        "fund", "fund_and_continue", "reject",
    ]
    assert "fund" in approve_hint("budget")
    assert "edit" in approve_hint("debate_rounds")


@pytest.fixture()
def api_client(tmp_path: Any) -> Any:
    """API client whose stub always revises → debate exhausts → HITL."""
    settings = _settings(max_debate_rounds=1)
    checkpointer = MemorySaver()
    graph = build_graph(
        settings, llm=_stub_llm(verdict="revise"),
        search=_FakeSearch(), scraper=_FakeScraper(), checkpointer=checkpointer,
    )
    db = str(tmp_path / "hitl.sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.runner = runner
    app.state.job_store = runner.job_store
    app.state.usage_store = runner.usage_store
    app.state.checkpointer = checkpointer
    with TestClient(app) as client:
        yield client


def _wait_status(client: Any, job_id: str, wanted: str,
                 timeout: float = 15.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/orchestration/reports/{job_id}").json()
        if body["status"] == wanted:
            return body
        if body["status"] in ("failed", "blocked") and wanted != body["status"]:
            raise AssertionError(f"job went {body['status']}, wanted {wanted}")
        time.sleep(0.05)
    raise AssertionError(f"job never reached {wanted}")


def test_poll_exposes_allowed_actions(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "hitl actions"})
    assert r.status_code == 202
    job = _wait_status(api_client, r.json()["job_id"], "awaiting_review")
    assert job["result"]["escalation_reason"] == "debate_rounds"
    assert job["result"]["has_draft"] is True
    assert set(job["result"]["allowed_actions"]) == {
        "approve", "edit", "reject"}


def test_approve_rejected_when_no_draft_budget(api_client: Any) -> None:
    """budget_exceeded with no draft → approve gets a guided 422."""
    r = api_client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "budget hitl", "budget_usd": 0.0})
    assert r.status_code == 202
    job = _wait_status(api_client, r.json()["job_id"], "awaiting_review")
    assert job["result"]["escalation_reason"] == "budget"
    assert job["result"]["has_draft"] is False
    assert "approve" not in job["result"]["allowed_actions"]

    resp = api_client.post(
        f"/api/v1/orchestration/reports/{job['job_id']}/review",
        json={"action": "approve"})
    assert resp.status_code == 422
    assert "fund" in resp.json()["detail"]


def test_fund_requires_positive_budget(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "fund zero", "budget_usd": 0.0})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "awaiting_review")
    resp = api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review",
        json={"action": "fund", "additional_budget_usd": 0.0})
    assert resp.status_code == 422
    assert "additional_budget_usd" in resp.json()["detail"]


def test_fund_and_continue_alias_completes(api_client: Any) -> None:
    """fund_and_continue resumes paid work; with a revising reviewer the
    job re-escalates on debate_rounds — now with a draft to approve."""
    r = api_client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "fund continue", "budget_usd": 0.0})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "awaiting_review")
    resp = api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review",
        json={"action": "fund_and_continue", "additional_budget_usd": 0.5})
    assert resp.status_code == 200
    job = _wait_status(api_client, job_id, "awaiting_review")
    # Funded work ran the full pipeline: a draft now exists and approve is
    # a legal action on this second escalation.
    assert job["result"]["escalation_reason"] == "debate_rounds"
    assert job["result"]["has_draft"] is True
    assert "approve" in job["result"]["allowed_actions"]

    api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review",
        json={"action": "approve"})
    final = _wait_status(api_client, job_id, "done")
    assert final["result"]["report"]


def test_double_resume_is_rejected(api_client: Any) -> None:
    """The first resume flips status synchronously; the second POST → 409."""
    r = api_client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "double resume", "budget_usd": 0.0})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "awaiting_review")
    decision = {"action": "fund", "additional_budget_usd": 0.5}
    first = api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review", json=decision)
    assert first.status_code == 200
    second = api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review", json=decision)
    assert second.status_code == 409
    # The revising stub re-escalates on debate_rounds — a single resume ran.
    _wait_status(api_client, job_id, "awaiting_review")


async def test_resume_after_runner_restart(api_client: Any) -> None:
    """A fresh JobRunner over the same checkpointer resumes the paused job."""
    r = api_client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "restart resume", "budget_usd": 0.0})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "awaiting_review")

    app = api_client.app
    settings = app.dependency_overrides[get_settings]()
    old_runner: JobRunner = app.state.runner
    # Simulated restart: new runner, same checkpointer + stores, no in-memory
    # trace/event bookkeeping carried over.
    new_graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(), scraper=_FakeScraper(),
        checkpointer=app.state.checkpointer,
    )
    new_runner = JobRunner(
        new_graph, old_runner.job_store, old_runner.usage_store, settings)
    # Drive the resume directly: polling the sync client would block this
    # test's event loop and starve the resume task.
    await new_runner._resume(
        job_id, "default",
        {"action": "fund", "additional_budget_usd": 0.5})
    rec = await old_runner.job_store.get("default", job_id)
    assert rec is not None and rec.status == "done"
    assert rec.result is not None and rec.result["report"]


async def test_expire_stale_marks_old_jobs(tmp_path: Any) -> None:
    settings = _settings(hitl_expiry_hours=72.0)
    db = str(tmp_path / "exp.sqlite")
    store = JobStore(db)
    runner = JobRunner(
        build_graph(settings, llm=_stub_llm(),
                    search=_FakeSearch(), scraper=_FakeScraper()),  # type: ignore[arg-type]
        store, UsageStore(db), settings)

    old = await store.create("ws-a", _brief("old job"))
    await store.update_status("ws-a", old.job_id, "awaiting_review")
    fresh = await store.create("ws-b", _brief("fresh job"))
    await store.update_status("ws-b", fresh.job_id, "awaiting_review")

    # Backdate the old job's updated_at past the expiry horizon.
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
            ((datetime.now(UTC) - timedelta(hours=80)).isoformat(),
             old.job_id),
        )
        await conn.commit()

    expired = await runner.expire_stale(now=datetime.now(UTC))
    assert [j.job_id for j in expired] == [old.job_id]

    old_rec = await store.get("ws-a", old.job_id)
    assert old_rec is not None and old_rec.status == "expired"
    assert old_rec.result is not None and old_rec.result["expired"] is True
    assert "72" in str(old_rec.result["expired_reason"])
    fresh_rec = await store.get("ws-b", fresh.job_id)
    assert fresh_rec is not None and fresh_rec.status == "awaiting_review"

    # SSE subscribers see a terminal expiry event.
    assert any(
        e.get("node") == "hitl_sweep" and e.get("terminal")
        for e in await runner.events(old.job_id)
    )


async def test_expire_stale_disabled(tmp_path: Any) -> None:
    settings = _settings(hitl_expiry_hours=0.0)
    db = str(tmp_path / "exp0.sqlite")
    store = JobStore(db)
    runner = JobRunner(
        build_graph(settings, llm=_stub_llm(),
                    search=_FakeSearch(), scraper=_FakeScraper()),  # type: ignore[arg-type]
        store, UsageStore(db), settings)
    job = await store.create("ws", _brief("expiry disabled"))
    await store.update_status("ws", job.job_id, "awaiting_review")
    assert await runner.expire_stale(now=datetime.now(UTC)) == []


def _brief(topic: str) -> Any:
    from app.graph.state import Brief

    return Brief(topic=topic)


def test_reject_reason_surfaces_in_result(api_client: Any) -> None:
    """Rejection is auditable: reason lands in result + hash-chained transcript."""
    r = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "reject reason"})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "awaiting_review")
    api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review",
        json={"action": "reject", "feedback": "scope too narrow"})
    job = _wait_status(api_client, job_id, "failed")
    assert job["result"]["rejection_reason"] == "scope too narrow"
    messages = [
        m["content"] for m in job["result"]["transcript"]
    ]
    assert any("scope too narrow" in c for c in messages)
    # The audit endpoint re-chains the same transcript — the reason lives
    # inside hashed message content, and the chain still verifies.
    audit = api_client.get(
        f"/api/v1/orchestration/reports/{job_id}/audit")
    assert audit.status_code == 200
    body = audit.json()
    assert body["valid"] is True
    assert any(
        m["sender"] == "human" for m in job["result"]["transcript"]
    )
