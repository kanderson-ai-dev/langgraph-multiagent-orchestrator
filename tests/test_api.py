"""Async job API: submit/poll/stream/review, multi-tenant isolation."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


@pytest.fixture()
def api_client(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """TestClient with the graph + stores wired for fully-offline operation."""
    settings = Settings(
        _env_file=None, scrape_delay_seconds=0.0, scrape_respect_robots=False
    )
    graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(), scraper=_FakeScraper(), checkpointer=None,
    )
    db = str(tmp_path / "test.sqlite")

    def _settings() -> Settings:
        return settings

    app = create_app()
    app.dependency_overrides[get_settings] = _settings
    job_store = JobStore(db)
    usage_store = UsageStore(db)
    runner = JobRunner(graph, job_store, usage_store, settings)
    app.state.runner = runner
    app.state.job_store = job_store
    app.state.usage_store = usage_store

    with TestClient(app) as client:
        yield client


def _submit(client: Any, topic: str = "B2B SaaS pricing trends") -> str:
    r = client.post("/api/v1/orchestration/reports", json={"topic": topic})
    assert r.status_code == 202
    return r.json()["job_id"]


def _wait_terminal(client: Any, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/orchestration/reports/{job_id}")
        body = r.json()
        if body["status"] in ("done", "failed", "awaiting_review", "blocked"):
            return body
        time.sleep(0.05)
    raise AssertionError("job never reached a terminal state")


def test_submit_poll_done(api_client: Any) -> None:
    job_id = _submit(api_client)
    final = _wait_terminal(api_client, job_id)
    assert final["status"] == "done"
    assert final["result"]["report"]
    assert final["result"]["transcript"]


def test_report_pdf_download(api_client: Any) -> None:
    job_id = _submit(api_client)
    _wait_terminal(api_client, job_id)
    r = api_client.get(f"/api/v1/orchestration/reports/{job_id}/report.pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"


def test_stream_emits_node_events(api_client: Any) -> None:
    job_id = _submit(api_client)
    _wait_terminal(api_client, job_id)
    r = api_client.get(f"/api/v1/orchestration/reports/{job_id}/stream")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert '"node": "supervisor"' in r.text or '"node": "writer"' in r.text


def test_404_unknown_job(api_client: Any) -> None:
    r = api_client.get("/api/v1/orchestration/reports/nonexistent")
    assert r.status_code == 404


def test_review_conflict_when_not_awaiting(api_client: Any) -> None:
    job_id = _submit(api_client)
    _wait_terminal(api_client, job_id)  # done, not awaiting
    r = api_client.post(
        f"/api/v1/orchestration/reports/{job_id}/review", json={"action": "approve"}
    )
    assert r.status_code == 409


def test_workspace_isolation(api_client: Any) -> None:
    """A job submitted in ws-A must be invisible to ws-B via a different token."""
    settings = Settings(
        _env_file=None,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
    )
    # Auth-enabled app: login as two workspaces.
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(), scraper=_FakeScraper(), checkpointer=None,
    )
    import tempfile

    db = tempfile.mktemp(suffix=".sqlite")
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)

    with TestClient(app) as c:
        tok_a = c.post("/api/v1/auth/login", json={
            "username": "admin", "password": "pw", "workspace_id": "ws-a"
        }).json()["access_token"]
        tok_b = c.post("/api/v1/auth/login", json={
            "username": "admin", "password": "pw", "workspace_id": "ws-b"
        }).json()["access_token"]

        r = c.post(
            "/api/v1/orchestration/reports",
            json={"topic": "secret project"},
            headers={"Authorization": f"Bearer {tok_a}"},
        )
        job_id = r.json()["job_id"]

        # ws-b cannot see ws-a's job.
        r_b = c.get(
            f"/api/v1/orchestration/reports/{job_id}",
            headers={"Authorization": f"Bearer {tok_b}"},
        )
        assert r_b.status_code == 404

        # No token → 401 when auth enabled.
        r_no = c.get(f"/api/v1/orchestration/reports/{job_id}")
        assert r_no.status_code == 401


def test_dashboard_summary(api_client: Any) -> None:
    r = api_client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert "total_cost_usd" in body and "cost_by_role" in body
