"""Append-only audit anchor log — the minimum viable external anchor.

Each sealed job appends one JSON line binding ``job_id`` to its
``audit_root`` (and signature, when the deployment signs). The file is
independent of the job database: it can be copied off-box, committed to a
transparency log, or compared later — a DB rewrite of ``result.audit_root``
no longer matches the preserved anchor.

Format (one line per sealed job, append-only):

    {"job_id": ..., "workspace_id": ..., "audit_root": ...,
     "audit_signature": ..., "sealed_at": "…Z"}

Real third-party timestamping (an RFC 3161 TSA or OpenTimestamps proof
over the root) is the documented next step — this file gives it a stable,
hash-addressed artifact to stamp.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def append_anchor(
    path: str | Path,
    *,
    job_id: str,
    workspace_id: str,
    audit_root: str,
    audit_signature: str | None,
    sealed_at: datetime | None = None,
) -> dict[str, Any]:
    """Append one anchor line; returns the written entry.

    The write goes through a fsync'd ``open("a")`` — single-writer
    process, one line per seal, so no locking is needed.
    """
    entry = {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "audit_root": audit_root,
        "audit_signature": audit_signature,
        "sealed_at": (sealed_at or datetime.now(UTC)).isoformat(),
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def read_anchors(path: str | Path) -> list[dict[str, Any]]:
    """Read the log back — missing file or corrupt lines yield nothing
    rather than raise (a half-written tail must not brick verification)."""
    p = Path(path)
    if not p.exists():
        return []
    anchors: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "job_id" in row:
            anchors.append(row)
    return anchors


def check_anchor(path: str | Path, *, job_id: str, audit_root: str) -> bool | None:
    """Compare a job's stored root against the preserved anchor.

    ``None`` when the job has no anchor entry (pre-anchor deployments,
    pruned log) — the check is *informative only* in that case.
    """
    for row in reversed(read_anchors(path)):
        if row.get("job_id") == job_id:
            return row.get("audit_root") == audit_root
    return None


def check_db(
    anchor_path: str | Path, db_path: str | Path
) -> list[dict[str, Any]]:
    """Compare every anchor against the jobs DB — the drift report for
    ``scripts/audit_anchor.py``. Returns one row per anchor with
    ``stored_root`` and ``match`` fields."""
    import sqlite3

    anchors = read_anchors(anchor_path)
    if not anchors or not Path(db_path).exists():
        return [{**a, "stored_root": None, "match": None} for a in anchors]
    conn = sqlite3.connect(str(db_path))
    try:
        rows = []
        for a in anchors:
            rec = conn.execute(
                "SELECT result_json FROM jobs WHERE job_id = ?",
                (a["job_id"],),
            ).fetchone()
            stored = None
            if rec and rec[0]:
                try:
                    stored = json.loads(rec[0]).get("audit_root")
                except json.JSONDecodeError:
                    stored = None
            rows.append(
                {
                    **a,
                    "stored_root": stored,
                    "match": (
                        None if stored is None else stored == a["audit_root"]
                    ),
                }
            )
        return rows
    finally:
        conn.close()


def latest(path: str | Path, *, job_id: str | None = None) -> dict[str, Any] | None:
    """Most recent anchor (optionally for one job) — what the helper
    script prints so the owner can publish the root off-box."""
    anchors = read_anchors(path)
    if job_id is not None:
        anchors = [a for a in anchors if a.get("job_id") == job_id]
    return anchors[-1] if anchors else None
