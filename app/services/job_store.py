"""SQLite job store — multi-tenant by design.

Every job carries a ``workspace_id``; all reads are scoped by it so one
tenant can never observe or operate on another tenant's jobs.
"""

from __future__ import annotations

import builtins
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

from app.graph.state import Brief, JobStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    status       TEXT NOT NULL,
    brief_json   TEXT NOT NULL,
    result_json  TEXT,
    cost_usd     REAL NOT NULL DEFAULT 0.0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id, created_at DESC);
"""


class JobRecord(BaseModel):
    job_id: str
    workspace_id: str
    status: JobStatus
    brief: Brief
    result: dict[str, object] | None = None
    cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        workspace_id=row["workspace_id"],
        status=row["status"],
        brief=Brief.model_validate(json.loads(row["brief_json"])),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        cost_usd=row["cost_usd"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class JobStore:
    """Async SQLite-backed store. One instance per database file."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if not self._initialized:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.executescript(_SCHEMA)
                await db.commit()
            self._initialized = True

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        await self._ensure_schema()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def create(self, workspace_id: str, brief: Brief) -> JobRecord:
        job_id = str(uuid.uuid4())
        now = _now()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO jobs (job_id, workspace_id, status, brief_json,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, workspace_id, "queued", brief.model_dump_json(), now, now),
            )
            await db.commit()
        record = await self.get(workspace_id, job_id)
        assert record is not None  # just inserted in this workspace
        return record

    async def get(self, workspace_id: str, job_id: str) -> JobRecord | None:
        """Fetch a job scoped to ``workspace_id`` — cross-tenant reads return None."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND workspace_id = ?",
                (job_id, workspace_id),
            )
            row = await cursor.fetchone()
        return _row_to_record(row) if row else None

    async def list_stale_awaiting_review(
        self, before: datetime
    ) -> list[JobRecord]:
        """All ``awaiting_review`` jobs (every workspace) idle since ``before``.

        Cross-workspace by design — only the background HITL sweeper calls
        this; it never flows back to a tenant-scoped request.
        """
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM jobs WHERE status = 'awaiting_review'"
                " AND updated_at < ? ORDER BY updated_at",
                (before.isoformat(),),
            )
            rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def mark_expired(
        self, workspace_id: str, job_id: str, *, reason: str
    ) -> JobRecord | None:
        """Terminal ``expired`` transition — merges the reason into result."""
        record = await self.get(workspace_id, job_id)
        if record is None or record.status != "awaiting_review":
            return None
        result = dict(record.result or {})
        result.update(
            {
                "expired": True,
                "expired_reason": reason,
                "escalation_reason": result.get("escalation_reason"),
            }
        )
        return await self.set_result(
            workspace_id, job_id, result, cost_usd=record.cost_usd, status="expired"
        )

    async def list(self, workspace_id: str, *, limit: int = 50) -> list[JobRecord]:
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
                (workspace_id, limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def status_counts(self, workspace_id: str) -> dict[str, int]:
        """Jobs by status for one tenant — dashboard performance surface."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*) AS n FROM jobs"
                " WHERE workspace_id = ? GROUP BY status",
                (workspace_id,),
            )
            rows = await cursor.fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def daily_stats(
        self, workspace_id: str, *, days: int = 14
    ) -> builtins.list[dict[str, object]]:
        # ``builtins.list`` — the JobStore.list method shadows the builtin
        # inside this class body.
        """Jobs + spend grouped by UTC day, newest last."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS jobs,"
                " COALESCE(SUM(cost_usd), 0.0) AS cost"
                " FROM jobs WHERE workspace_id = ?"
                " AND created_at >= date('now', ?)"
                " GROUP BY day ORDER BY day",
                (workspace_id, f"-{days} days"),
            )
            rows = await cursor.fetchall()
        return [
            {"date": r["day"], "jobs": int(r["jobs"]), "cost_usd": float(r["cost"])}
            for r in rows
        ]

    async def escalation_reasons(self, workspace_id: str) -> dict[str, int]:
        """Escalation reasons for non-terminal jobs (from result JSON)."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT json_extract(result_json, '$.escalation_reason') AS r,"
                " COUNT(*) AS n FROM jobs WHERE workspace_id = ?"
                " AND status IN ('awaiting_review', 'expired')"
                " GROUP BY r",
                (workspace_id,),
            )
            rows = await cursor.fetchall()
        return {
            str(r["r"] or "unknown"): int(r["n"]) for r in rows
        }

    async def budget_usage(self, workspace_id: str) -> dict[str, float]:
        """Aggregate budget vs spend across jobs that declared a cap."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n,"
                " COALESCE(SUM(json_extract(brief_json, '$.budget_usd')), 0.0) AS b,"
                " COALESCE(SUM(cost_usd), 0.0) AS c FROM jobs"
                " WHERE workspace_id = ?"
                " AND json_extract(brief_json, '$.budget_usd') IS NOT NULL",
                (workspace_id,),
            )
            row = await cursor.fetchone()
        return {
            "jobs_with_budget": int(row["n"]) if row else 0,
            "budget_usd": float(row["b"]) if row else 0.0,
            "spent_usd": float(row["c"]) if row else 0.0,
        }

    async def update_status(
        self, workspace_id: str, job_id: str, status: JobStatus
    ) -> JobRecord | None:
        async with self._conn() as db:
            await db.execute(
                "UPDATE jobs SET status = ?, updated_at = ?"
                " WHERE job_id = ? AND workspace_id = ?",
                (status, _now(), job_id, workspace_id),
            )
            await db.commit()
        return await self.get(workspace_id, job_id)

    async def set_result(
        self,
        workspace_id: str,
        job_id: str,
        result: dict[str, object],
        *,
        cost_usd: float | None = None,
        status: JobStatus | None = None,
    ) -> JobRecord | None:
        async with self._conn() as db:
            await db.execute(
                "UPDATE jobs SET result_json = ?, cost_usd = ?, status = ?,"
                " updated_at = ? WHERE job_id = ? AND workspace_id = ?",
                (
                    json.dumps(result),
                    cost_usd if cost_usd is not None else 0.0,
                    status or "done",
                    _now(),
                    job_id,
                    workspace_id,
                ),
            )
            await db.commit()
        return await self.get(workspace_id, job_id)
