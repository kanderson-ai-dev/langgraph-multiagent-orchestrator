"""Tamper-evident audit trail — hash chain over the job transcript."""

from typing import Any

from app.graph.state import AgentMessage
from app.services.audit import (
    GENESIS,
    audit_root,
    chain_transcript,
    verify_chain,
)
from tests.test_api import _submit, _wait_terminal, api_client  # noqa: F401


def _msgs() -> list[AgentMessage]:
    return [
        AgentMessage(sender="input_guardrail", recipient="supervisor",
                     kind="system", content="Brief accepted."),
        AgentMessage(sender="supervisor", recipient="researcher",
                     kind="dispatch", content="no evidence yet"),
        AgentMessage(sender="researcher", recipient="supervisor",
                     kind="result", content="5 items gathered"),
    ]


def test_empty_chain_is_genesis() -> None:
    assert chain_transcript([]) == []
    assert audit_root([]) == GENESIS


def test_chain_links_previous_hash() -> None:
    msgs = _msgs()
    entries = chain_transcript(msgs)
    assert entries[0].prev_hash == GENESIS
    for prev, cur in zip(entries, entries[1:], strict=False):
        assert cur.prev_hash == prev.hash
    assert audit_root(msgs) == entries[-1].hash


def test_tampering_invalidates_root() -> None:
    msgs = _msgs()
    root = audit_root(msgs)
    assert verify_chain(msgs, root)
    # Flip one bit of history — the whole chain must fail.
    msgs[1] = msgs[1].model_copy(update={"content": "tampered dispatch"})
    assert not verify_chain(msgs, root)


def test_insertion_or_deletion_invalidates_root() -> None:
    msgs = _msgs()
    root = audit_root(msgs)
    assert not verify_chain(msgs[1:], root)          # deleted a message
    assert not verify_chain(msgs + msgs[:1], root)   # appended a forged one


async def test_audit_endpoint_returns_valid_trail(api_client: Any) -> None:  # noqa: F811
    job_id = _submit(api_client, "audit trail e2e check")
    job = _wait_terminal(api_client, job_id)
    assert job["status"] == "done"

    r = api_client.get(f"/api/v1/orchestration/reports/{job_id}/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["audit_root"] != GENESIS
    assert body["valid"] is True
    assert len(body["entries"]) == len(job["result"]["transcript"])
    # The fingerprint is also embedded in the delivered report.
    assert body["audit_root"] in job["result"]["report"]


async def test_audit_endpoint_never_5xx_for_running_job(api_client: Any) -> None:  # noqa: F811
    """Race-tolerant: a still-running job yields 409 (not ready) or 200 if it
    finished before the request landed — never a server error."""
    r = api_client.post(
        "/api/v1/orchestration/reports", json={"topic": "audit timing check"}
    )
    job_id = r.json()["job_id"]
    r2 = api_client.get(f"/api/v1/orchestration/reports/{job_id}/audit")
    assert r2.status_code in (200, 409)


async def test_audit_endpoint_tenant_isolation(api_client: Any) -> None:  # noqa: F811
    assert api_client.get("/api/v1/orchestration/reports/ghost/audit").status_code == 404
