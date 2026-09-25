"""SQLite job store — multi-tenant by design.

Every job carries a ``workspace_id``; all reads are scoped by it so one
tenant can never observe or operate on another tenant's jobs.
"""

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

    async def list(self, workspace_id: str, *, limit: int = 50) -> list[JobRecord]:
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
                (workspace_id, limit),
            )
            rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

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
