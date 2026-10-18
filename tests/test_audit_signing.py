"""HMAC-signed audit roots (Phase: audit authenticity).

The plain hash chain proves *consistency* — anyone can recompute it. The
HMAC proves *authenticity*: an attacker with write access to the DB could
forge a self-consistent chain (edit the transcript, recompute the root)
but could not mint the signature bound to the deployment's key.
"""

import json
import sqlite3
import time
from typing import Any

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.main import create_app
from app.services.audit import (
    audit_root,
    sign_root,
    verify_signature,
)
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.prompt_registry import PromptRegistry
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm

KEY = "test-audit-key-0123456789abcdef"


def _app(tmp_path: Any, *, signing_key: str | None = KEY) -> Any:
    settings = Settings(
        _env_file=None,
        scrape_delay_seconds=0.0,
        scrape_respect_robots=False,
        audit_signing_key=SecretStr(signing_key) if signing_key else None,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    from app.graph.graph import build_graph

    db = str(tmp_path / "audit.sqlite")
    graph = build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=MemorySaver(),
    )
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    return app


def _done_job(client: TestClient) -> str:
    r = client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "signed audit trail", "budget_usd": 1.0},
    )
    job_id = r.json()["job_id"]
    for _ in range(300):
        body = client.get(f"/api/v1/orchestration/reports/{job_id}").json()
        if body["status"] in ("done", "failed", "blocked"):
            return job_id
        time.sleep(0.05)
    raise AssertionError("job never finished")


def _audit(client: TestClient, job_id: str) -> dict[str, Any]:
    r = client.get(f"/api/v1/orchestration/reports/{job_id}/audit")
    assert r.status_code == 200, r.text
    return r.json()


def _tamper(db: str, job_id: str, mutate: Any) -> None:
    """Rewrite the job's result_json as a DB-level attacker would."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT result_json FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        result = mutate(json.loads(row[0]))
        conn.execute(
            "UPDATE jobs SET result_json = ? WHERE job_id = ?",
            (json.dumps(result), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_sign_and_verify_roundtrip() -> None:
    sig = sign_root(KEY, "ab12" * 16, job_id="j1", workspace_id="w1")
    assert verify_signature(KEY, sig, "ab12" * 16, job_id="j1", workspace_id="w1")
    # Same root, different scope → invalid (roots can't be replayed across
    # jobs or tenants).
    assert not verify_signature(
        KEY, sig, "ab12" * 16, job_id="j2", workspace_id="w1"
    )
    assert not verify_signature(
        "other-key", sig, "ab12" * 16, job_id="j1", workspace_id="w1"
    )


def test_signed_job_verifies(tmp_path: Any) -> None:
    with TestClient(_app(tmp_path)) as client:
        job_id = _done_job(client)
        audit = _audit(client, job_id)
        assert audit["valid"] is True
        assert audit["signed"] is True
        assert audit["signature_valid"] is True


def test_unsigned_deployment_reports_honestly(tmp_path: Any) -> None:
    with TestClient(_app(tmp_path, signing_key=None)) as client:
        job_id = _done_job(client)
        audit = _audit(client, job_id)
        assert audit["valid"] is True
        assert audit["signed"] is False
        assert audit["signature_valid"] is None


def test_forged_consistent_chain_fails_signature(tmp_path: Any) -> None:
    """The attack this phase exists for: DB writer edits the transcript AND
    recomputes a consistent root. Chain check passes; the HMAC doesn't."""
    db = str(tmp_path / "audit.sqlite")
    with TestClient(_app(tmp_path)) as client:
        job_id = _done_job(client)

        def forge(result: dict[str, Any]) -> dict[str, Any]:
            from app.graph.state import AgentMessage

            transcript = [
                AgentMessage.model_validate(m) for m in result["transcript"]
            ]
            # Drop the last message and recompute — a self-consistent forgery.
            forged = transcript[:-1]
            result["transcript"] = [
                m.model_dump(mode="json") for m in forged
            ]
            result["audit_root"] = audit_root(forged)
            return result

        _tamper(db, job_id, forge)
        audit = _audit(client, job_id)
        assert audit["valid"] is True  # forged chain is consistent
        assert audit["signature_valid"] is False  # but cannot be signed


def test_registry_chain_signed_per_event(tmp_path: Any) -> None:
    db = str(tmp_path / "registry.sqlite")
    reg = PromptRegistry.load(db_path=db, signing_key=KEY)
    reg.create_candidate("writer", "Signed candidate.", "audit sig test")
    valid, _root, entries = reg.verify_audit_chain()
    assert valid
    assert entries[-1]["signature_valid"] is True

    # Same DB, no key configured: hashes still verify, signatures unreadable.
    reg2 = PromptRegistry.load(db_path=db)
    valid2, _r2, entries2 = reg2.verify_audit_chain()
    assert valid2
    assert entries2[-1]["signature_valid"] is None


def test_evidence_root_full_hex_in_transcript(tmp_path: Any) -> None:
    """The audited chain carries the full 64-hex evidence root."""
    with TestClient(_app(tmp_path)) as client:
        job_id = _done_job(client)
        audit = _audit(client, job_id)
        root = audit["evidence_root"]
        assert root and len(root) == 64
        body = client.get(f"/api/v1/orchestration/reports/{job_id}").json()
        sealed = [
            m for m in body["result"]["transcript"]
            if "Evidence sealed:" in (m.get("content") or "")
        ]
        assert sealed, "no evidence-seal message in transcript"
        assert any(root in m["content"] for m in sealed)
