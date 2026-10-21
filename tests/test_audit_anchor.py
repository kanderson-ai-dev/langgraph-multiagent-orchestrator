"""Local append-only audit anchor log (Phase: external anchoring).

The anchor file is the off-box copy: each sealed job appends one JSONL
line binding ``job_id`` to its ``audit_root``. If the job DB is later
rewritten, ``check_anchor`` catches the mismatch against the preserved
log. No network, no dependencies — a real TSA (RFC 3161/OpenTimestamps)
is documented as the next step.
"""

import json
from typing import Any

from fastapi.testclient import TestClient

from app.services.audit_anchor import (
    append_anchor,
    check_anchor,
    check_db,
    read_anchors,
)
from tests.test_audit_signing import _app, _audit, _done_job, _tamper


def test_append_writes_one_jsonl_line(tmp_path: Any) -> None:
    path = tmp_path / "anchor.jsonl"
    append_anchor(
        str(path), job_id="j1", workspace_id="w1",
        audit_root="ab" * 32, audit_signature="kid:" + "cd" * 32,
    )
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["job_id"] == "j1"
    assert entry["workspace_id"] == "w1"
    assert entry["audit_root"] == "ab" * 32
    assert entry["audit_signature"] == "kid:" + "cd" * 32
    assert "sealed_at" in entry


def test_anchors_are_append_only_and_ordered(tmp_path: Any) -> None:
    path = str(tmp_path / "anchor.jsonl")
    for i in range(3):
        append_anchor(
            path, job_id=f"j{i}", workspace_id="w",
            audit_root=f"root{i}", audit_signature=None,
        )
    anchors = read_anchors(path)
    assert [a["job_id"] for a in anchors] == ["j0", "j1", "j2"]


def test_check_anchor_detects_rewritten_root(tmp_path: Any) -> None:
    path = str(tmp_path / "anchor.jsonl")
    append_anchor(
        path, job_id="j1", workspace_id="w",
        audit_root="aa" * 32, audit_signature=None,
    )
    assert check_anchor(path, job_id="j1", audit_root="aa" * 32) is True
    assert check_anchor(path, job_id="j1", audit_root="bb" * 32) is False


def test_check_anchor_unknown_job_returns_none(tmp_path: Any) -> None:
    path = str(tmp_path / "anchor.jsonl")
    append_anchor(
        path, job_id="j1", workspace_id="w",
        audit_root="aa" * 32, audit_signature=None,
    )
    assert check_anchor(path, job_id="nope", audit_root="aa" * 32) is None


def test_missing_or_corrupt_anchor_file(tmp_path: Any) -> None:
    missing = str(tmp_path / "nope.jsonl")
    assert read_anchors(missing) == []
    path = tmp_path / "anchor.jsonl"
    path.write_text('{"job_id":"j1","audit_root":"x"}\nnot json\n')
    assert len(read_anchors(str(path))) == 1


def test_sealed_job_writes_anchor_and_detects_tamper(tmp_path: Any) -> None:
    """End-to-end: a finished job appends exactly one anchor line; a DB
    rewrite of the stored root is caught against the preserved log."""
    path = str(tmp_path / "anchor.jsonl")
    db = str(tmp_path / "audit.sqlite")
    with TestClient(_app(tmp_path)) as client:
        job_id = _done_job(client)
    anchors = read_anchors(path)
    assert len(anchors) == 1
    assert anchors[0]["job_id"] == job_id
    real_root = anchors[0]["audit_root"]
    assert check_anchor(path, job_id=job_id, audit_root=real_root) is True

    def forge(result: dict[str, Any]) -> dict[str, Any]:
        result["audit_root"] = "ff" * 32
        return result

    _tamper(db, job_id, forge)
    with TestClient(_app(tmp_path)) as client:
        audit = _audit(client, job_id)
        # Chain check fails (root no longer matches the transcript) AND
        # the anchor mismatch is surfaced independently.
        assert audit["valid"] is False
        assert audit["anchor_match"] is False


def test_check_db_reports_drift(tmp_path: Any) -> None:
    """``check_db`` (the helper script's code path) flags a rewritten
    stored root as drift and passes an untouched one."""
    path = str(tmp_path / "anchor.jsonl")
    db = str(tmp_path / "audit.sqlite")
    with TestClient(_app(tmp_path)) as client:
        job_id = _done_job(client)
    rows = check_db(path, db)
    assert len(rows) == 1 and rows[0]["match"] is True

    _tamper(db, job_id, lambda r: {**r, "audit_root": "ee" * 32})
    rows = check_db(path, db)
    assert rows[0]["match"] is False
