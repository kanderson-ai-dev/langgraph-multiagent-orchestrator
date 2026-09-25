"""Usage store — per-job, per-agent-role token and cost accounting.

Only numeric metadata is persisted (tokens, cost, latency) — never prompt or
report content.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    event_id          TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    role              TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    latency_ms        REAL NOT NULL DEFAULT 0.0,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_job ON usage_events(job_id);
CREATE INDEX IF NOT EXISTS idx_usage_workspace ON usage_events(workspace_id);
"""


class UsageEvent(BaseModel):
    event_id: str
    job_id: str
    workspace_id: str
    role: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    created_at: datetime


class UsageSummary(BaseModel):
    total_cost_usd: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    avg_latency_ms: float = 0.0
    event_count: int = 0
    by_role: dict[str, float] = {}


class UsageStore:
    """Async SQLite-backed usage accounting."""

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

    async def record(
        self,
        *,
        job_id: str,
        workspace_id: str,
        role: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
    ) -> UsageEvent:
        event = UsageEvent(
            event_id=str(uuid.uuid4()),
            job_id=job_id,
            workspace_id=workspace_id,
            role=role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            created_at=datetime.now(UTC),
        )
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO usage_events (event_id, job_id, workspace_id, role,"
                " model, prompt_tokens, completion_tokens, cost_usd, latency_ms,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.job_id,
                    event.workspace_id,
                    event.role,
                    event.model,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.cost_usd,
                    event.latency_ms,
                    event.created_at.isoformat(),
                ),
            )
            await db.commit()
        return event

    async def job_cost(self, job_id: str) -> float:
        """Total cost in USD attributed to a job."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM usage_events"
                " WHERE job_id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
        return float(row["total"]) if row else 0.0

    async def workspace_summary(self, workspace_id: str) -> UsageSummary:
        """Aggregate cost/latency summary for one tenant."""
        async with self._conn() as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total_cost,"
                " COALESCE(SUM(prompt_tokens), 0) AS pt,"
                " COALESCE(SUM(completion_tokens), 0) AS ct,"
                " COALESCE(AVG(latency_ms), 0.0) AS avg_lat,"
                " COUNT(*) AS n FROM usage_events WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
            by_role_cursor = await db.execute(
                "SELECT role, SUM(cost_usd) AS c FROM usage_events"
                " WHERE workspace_id = ? GROUP BY role",
                (workspace_id,),
            )
            role_rows = await by_role_cursor.fetchall()
        if not row or row["n"] == 0:
            return UsageSummary()
        return UsageSummary(
            total_cost_usd=float(row["total_cost"]),
            total_prompt_tokens=int(row["pt"]),
            total_completion_tokens=int(row["ct"]),
            avg_latency_ms=float(row["avg_lat"]),
            event_count=int(row["n"]),
            by_role={r["role"]: float(r["c"]) for r in role_rows},
        )
