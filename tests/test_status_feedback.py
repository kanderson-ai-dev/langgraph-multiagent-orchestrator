"""Phase 13: public /status + /eval/latest, feedback queue, report exports,
white-label config."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings, get_settings
from app.graph.graph import build_graph
from app.main import create_app
from app.services.feedback_store import FeedbackStore
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


@pytest.fixture()
def api_client(tmp_path: Any) -> Any:
    settings = _settings()
    graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(), scraper=_FakeScraper(),
        checkpointer=MemorySaver(),
    )
    db = str(tmp_path / "p13.sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.runner = runner
    app.state.job_store = runner.job_store
    app.state.usage_store = runner.usage_store
    app.state.feedback_store = FeedbackStore(db)
    with TestClient(app) as client:
        yield client


def _wait_status(client: Any, job_id: str, wanted: str,
                 timeout: float = 20.0) -> dict[str, Any]:
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


def test_public_status_is_payload_free(api_client: Any) -> None:
    r = api_client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["environment"] == "development"
    assert isinstance(body["uptime_seconds"], (int, float))
    assert isinstance(body["jobs"], dict)
    # Payload-free contract: no workspace ids, key names or internals.
    assert "workspace" not in str(body).lower() or "workspace_id" not in body


def test_eval_latest_serves_committed_scorecard(api_client: Any) -> None:
    r = api_client.get("/eval/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["citation_support_rate"] == 1.0
    assert set(body["gates"]) >= {"citation_support_rate", "judge_score_avg"}


def test_ui_config_is_public_and_branded(api_client: Any) -> None:
    r = api_client.get("/api/v1/config/ui")
    assert r.status_code == 200
    assert r.json() == {
        "product_name": "Orchestrator",
        "accent_color": "#4f7cff",
        "logo_url": None,
    }


def test_ui_config_reflects_overrides(tmp_path: Any) -> None:
    settings = _settings(
        ui_product_name="Acme Reports",
        ui_accent_color="#ff0066",
        ui_logo_url="https://cdn.example.com/logo.svg",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = str(tmp_path / "p13b.sqlite")
    app.state.job_store = JobStore(db)
    with TestClient(app) as client:
        body = client.get("/api/v1/config/ui").json()
    assert body["product_name"] == "Acme Reports"
    assert body["accent_color"] == "#ff0066"
    assert body["logo_url"] == "https://cdn.example.com/logo.svg"


def test_report_md_and_docx_exports(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "exports"})
    job_id = r.json()["job_id"]
    job = _wait_status(api_client, job_id, "done")

    md = api_client.get(f"/api/v1/orchestration/reports/{job_id}/report.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert job["result"]["report"].strip() in md.text

    docx = api_client.get(f"/api/v1/orchestration/reports/{job_id}/report.docx")
    assert docx.status_code == 200
    assert "wordprocessingml" in docx.headers["content-type"]
    assert docx.content[:2] == b"PK"  # a .docx is a zip


def test_report_export_409_when_not_ready(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "paused", "budget_usd": 0.0})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "awaiting_review")
    assert api_client.get(
        f"/api/v1/orchestration/reports/{job_id}/report.md").status_code == 409
    assert api_client.get(
        f"/api/v1/orchestration/reports/{job_id}/report.docx").status_code == 409


def test_feedback_queue_and_export(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "rated"})
    job_id = r.json()["job_id"]
    _wait_status(api_client, job_id, "done")

    fb = api_client.post(
        "/api/v1/feedback",
        json={"job_id": job_id, "rating": "up",
              "comment": "crisp executive summary", "section": "Summary"})
    assert fb.status_code == 201

    items = api_client.get("/api/v1/feedback").json()
    assert items["count"] == 1
    assert items["items"][0]["rating"] == "up"

    export = api_client.get("/api/v1/feedback/export").json()
    case = export["cases"][0]
    assert case["candidate_case"]["note"].startswith("reviewed manually")
    assert case["job_id"] == job_id


def test_feedback_unknown_job_is_404(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/feedback", json={"job_id": "nope", "rating": "up"})
    assert r.status_code == 404
    assert r.json()["detail"] == "job not found"


def test_feedback_rejects_bad_rating(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/feedback", json={"job_id": "x", "rating": "meh"})
    assert r.status_code == 422
