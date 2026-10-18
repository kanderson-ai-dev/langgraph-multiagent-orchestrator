"""Phase 6 — evidence sealing in the audit chain.

The manifest root is a content address of the gathered sources: same
content → same root, tampered content → different root. The seal is
folded into the audit chain before the writer runs, and the workspace-
scoped ``/evidence`` endpoint exposes the sealed view.
"""

from __future__ import annotations

import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.graph.evidence import build_evidence_manifest, evidence_sha
from app.graph.graph import build_graph
from app.graph.nodes.reviewer import verify_citations
from app.graph.state import Citation, EvidenceItem, ReportDraft
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _item(text: str = "alpha beta gamma", url: str = "https://a.test/x") -> EvidenceItem:
    return EvidenceItem(
        evidence_id="e1", url=url, title="t", text=text,
        sha256=evidence_sha(url, "text/html", text),
        content_type="text/html",
    )


def test_manifest_root_is_reproducible() -> None:
    a = build_evidence_manifest([_item()])
    b = build_evidence_manifest([_item()])
    assert a["root"] == b["root"]
    assert a["count"] == 1 and a["items"][0]["sha256"]


def test_manifest_root_is_order_independent() -> None:
    i1 = _item(url="https://a.test/1")
    i2 = _item(url="https://a.test/2")
    i2 = i2.model_copy(update={"evidence_id": "e2"})
    assert (
        build_evidence_manifest([i1, i2])["root"]
        == build_evidence_manifest([i2, i1])["root"]
    )


def test_tampered_evidence_changes_root() -> None:
    good = build_evidence_manifest([_item()])
    tampered = _item(text="alpha beta gamma ALTERED")
    tampered = tampered.model_copy(
        update={"sha256": evidence_sha(tampered.url, "text/html", tampered.text)}
    )
    assert build_evidence_manifest([tampered])["root"] != good["root"]


def test_verify_citations_returns_source_hashes() -> None:
    ev = _item()
    draft = ReportDraft(
        version=1, title="t", markdown="m",
        citations=[
            Citation(claim="c", quote="alpha beta gamma", source_url=ev.url),
            Citation(claim="c2", quote="zzz not present", source_url=ev.url),
        ],
    )
    kept, dropped, hashes, _unknown = verify_citations(draft, [ev])
    assert len(kept) == 1 and dropped == 1
    assert hashes == [ev.sha256]


def _app(settings: Settings) -> tuple[Any, str]:
    graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(), scraper=_FakeScraper(), checkpointer=None,
    )
    db = tempfile.mktemp(suffix=".sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = app.state.runner.job_store
    app.state.usage_store = app.state.runner.usage_store
    return app, db


@pytest.fixture()
def api_client(settings: Settings) -> Any:
    """Offline TestClient — same wiring as the API suite."""
    settings = settings.model_copy(
        update={"scrape_delay_seconds": 0.0, "scrape_respect_robots": False}
    )
    app, _db = _app(settings)
    with TestClient(app) as client:
        yield client


def test_evidence_sealed_and_served(api_client: Any) -> None:
    r = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "sealed sources"}
    )
    job_id = r.json()["job_id"]
    import time

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        body = api_client.get(f"/api/v1/orchestration/reports/{job_id}").json()
        if body["status"] in ("done", "failed", "blocked", "awaiting_review"):
            break
        time.sleep(0.05)
    assert body["status"] == "done"

    result = body["result"]
    assert result["evidence_root"] and result["evidence_manifest"]["count"] >= 1

    # The seal is part of the audited transcript.
    assert any(
        "Evidence sealed:" in m["content"]
        for m in result["transcript"]
    )
    audit = api_client.get(f"/api/v1/orchestration/reports/{job_id}/audit").json()
    assert audit["valid"] and audit["evidence_root"] == result["evidence_root"]

    ev = api_client.get(f"/api/v1/orchestration/reports/{job_id}/evidence").json()
    assert ev["evidence_root"] == result["evidence_root"]
    assert all(len(i["sha256"]) == 64 for i in ev["items"])

    # Footnotes carry the short source hash.
    assert "`#" in result["report"]


def test_evidence_route_cross_tenant_404() -> None:
    settings = Settings(
        _env_file=None, scrape_delay_seconds=0.0,
        scrape_respect_robots=False,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
    )
    app, _db = _app(settings)
    with TestClient(app) as c:
        tok_a = c.post("/api/v1/auth/login", json={
            "username": "admin", "password": "pw", "workspace_id": "ws-a"
        }).json()["access_token"]
        tok_b = c.post("/api/v1/auth/login", json={
            "username": "admin", "password": "pw", "workspace_id": "ws-b"
        }).json()["access_token"]
        r_sub = c.post(
            "/api/v1/orchestration/reports", json={"topic": "tenant test"},
            headers={"Authorization": f"Bearer {tok_a}"},
        )
        assert r_sub.status_code == 202, r_sub.text
        job_id = r_sub.json()["job_id"]
        r = c.get(
            f"/api/v1/orchestration/reports/{job_id}/evidence",
            headers={"Authorization": f"Bearer {tok_b}"},
        )
        assert r.status_code == 404


def test_evidence_route_not_sealed_409(api_client: Any) -> None:
    job_id = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "too early"}
    ).json()["job_id"]
    # Immediately after submit the run usually hasn't reached the seal —
    # the endpoint must say 409, not crash. If the stub was fast enough
    # to finish, accept that too.
    r = api_client.get(f"/api/v1/orchestration/reports/{job_id}/evidence")
    assert r.status_code in (200, 409)
