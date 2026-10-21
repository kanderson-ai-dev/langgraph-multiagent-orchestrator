"""Postgres-backed stores — the multi-replica backend.

One ``asyncpg.Pool`` (created lazily on first use) feeds all six stores,
mirroring the SQLite layout where every store shares ``database_path``.
Semantics match the SQLite classes exactly: tenant-scoped reads return
``None`` on cross-tenant access, JSON columns are ``jsonb``, timestamps
stay ISO text (lexicographic ordering is deliberate — the SQLite schema
made the same choice), and schemas auto-migrate on first use instead of
requiring a separate migration step.

Differences worth noting:

- Upserts use ``ON CONFLICT`` (SQLite ``INSERT OR IGNORE/REPLACE``).
- JSON paths use ``->>`` instead of ``json_extract``.
- ``feedback.id`` is ``GENERATED ALWAYS AS IDENTITY`` + ``RETURNING id``
  instead of ``AUTOINCREMENT`` + ``lastrowid``.
- Date cutoffs are computed in Python (identical string compare) rather
  than the ``date('now', ?)`` SQL function.

The LangGraph checkpointer is deliberately NOT part of this set — it
lives on ``checkpoint_db_path`` (SQLite) either way; a ``langgraph``
Postgres saver exists upstream and is a documented future step.
"""

from __future__ import annotations

import builtins
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from app.graph.state import Brief, JobStatus
from app.services.job_store import JobRecord
from app.services.usage_store import UsageEvent, UsageSummary
from app.services.workspace_store import (
    KEY_PREFIX,
    ROLES,
    ApiKeyRecord,
    hash_api_key,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    status       TEXT NOT NULL,
    brief_json   JSONB NOT NULL,
    result_json  JSONB,
    cost_usd     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id, created_at DESC);
CREATE TABLE IF NOT EXISTS usage_events (
    event_id          TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    role              TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    latency_ms        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    prompt_name       TEXT,
    prompt_version    INTEGER,
    prompt_sha256     TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_job ON usage_events(job_id);
CREATE INDEX IF NOT EXISTS idx_usage_workspace ON usage_events(workspace_id);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    workspace_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    password_hash TEXT,
    PRIMARY KEY (workspace_id, subject)
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    prefix TEXT NOT NULL,
    role TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE TABLE IF NOT EXISTS feedback (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    section TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_ws ON feedback(workspace_id, created_at);
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_cache (
    kind         TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    cache_key    TEXT NOT NULL,
    day          TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (kind, workspace_id, cache_key, day)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row: asyncpg.Record) -> JobRecord:
    result_raw = row["result_json"]
    return JobRecord(
        job_id=row["job_id"],
        workspace_id=row["workspace_id"],
        status=row["status"],
        brief=Brief.model_validate(json.loads(row["brief_json"])),
        result=json.loads(result_raw) if result_raw else None,
        cost_usd=row["cost_usd"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _affected(status_tag: str) -> int:
    """asyncpg returns "INSERT 0 1" / "UPDATE 3"-style tags."""
    return int(status_tag.rsplit(" ", 1)[-1])


class _PgBase:
    """Shared lazy pool + one-time schema application."""

    def __init__(self, holder: _PoolHolder) -> None:
        self._holder = holder

    async def _pool(self) -> asyncpg.Pool:
        return await self._holder.pool()


class _PoolHolder:
    """Owns the lazily-created pool + schema flag for all Pg stores."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._initialized = False

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=10
            )
        if not self._initialized:
            async with self._pool.acquire() as conn:
                await conn.execute(SCHEMA)
            self._initialized = True
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class PgJobStore(_PgBase):
    async def create(self, workspace_id: str, brief: Brief) -> JobRecord:
        job_id = str(uuid.uuid4())
        now = _now()
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO jobs (job_id, workspace_id, status, brief_json,"
                " created_at, updated_at) VALUES ($1, $2, $3, $4::jsonb, $5, $6)",
                job_id, workspace_id, "queued", brief.model_dump_json(), now, now,
            )
        record = await self.get(workspace_id, job_id)
        assert record is not None  # just inserted in this workspace
        return record

    async def get(self, workspace_id: str, job_id: str) -> JobRecord | None:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT * FROM jobs WHERE job_id = $1 AND workspace_id = $2",
                job_id, workspace_id,
            )
        return _row_to_record(row) if row else None

    async def list_stale_awaiting_review(
        self, before: datetime
    ) -> list[JobRecord]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT * FROM jobs WHERE status = 'awaiting_review'"
                " AND updated_at < $1 ORDER BY updated_at",
                before.isoformat(),
            )
        return [_row_to_record(r) for r in rows]

    async def mark_expired(
        self, workspace_id: str, job_id: str, *, reason: str
    ) -> JobRecord | None:
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

    async def list(
        self, workspace_id: str, *, limit: int = 50
    ) -> builtins.list[JobRecord]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT * FROM jobs WHERE workspace_id = $1"
                " ORDER BY created_at DESC LIMIT $2",
                workspace_id, limit,
            )
        return [_row_to_record(r) for r in rows]

    async def status_counts(self, workspace_id: str) -> dict[str, int]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT status, COUNT(*) AS n FROM jobs"
                " WHERE workspace_id = $1 GROUP BY status",
                workspace_id,
            )
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def status_counts_all(self) -> dict[str, int]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            )
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def daily_stats(
        self, workspace_id: str, *, days: int = 14
    ) -> builtins.list[dict[str, object]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS jobs,"
                " COALESCE(SUM(cost_usd), 0.0) AS cost"
                " FROM jobs WHERE workspace_id = $1"
                " AND created_at >= $2 GROUP BY day ORDER BY day",
                workspace_id, cutoff,
            )
        return [
            {"date": r["day"], "jobs": int(r["jobs"]), "cost_usd": float(r["cost"])}
            for r in rows
        ]

    async def escalation_reasons(self, workspace_id: str) -> dict[str, int]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT result_json ->> 'escalation_reason' AS r,"
                " COUNT(*) AS n FROM jobs WHERE workspace_id = $1"
                " AND status IN ('awaiting_review', 'expired')"
                " GROUP BY r",
                workspace_id,
            )
        return {str(r["r"] or "unknown"): int(r["n"]) for r in rows}

    async def budget_usage(self, workspace_id: str) -> dict[str, float]:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT COUNT(*) AS n,"
                " COALESCE(SUM((brief_json ->> 'budget_usd')::float), 0.0) AS b,"
                " COALESCE(SUM(cost_usd), 0.0) AS c FROM jobs"
                " WHERE workspace_id = $1"
                " AND brief_json ->> 'budget_usd' IS NOT NULL",
                workspace_id,
            )
        return {
            "jobs_with_budget": int(row["n"]) if row else 0,
            "budget_usd": float(row["b"]) if row else 0.0,
            "spent_usd": float(row["c"]) if row else 0.0,
        }

    async def update_status(
        self, workspace_id: str, job_id: str, status: JobStatus
    ) -> JobRecord | None:
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "UPDATE jobs SET status = $1, updated_at = $2"
                " WHERE job_id = $3 AND workspace_id = $4",
                status, _now(), job_id, workspace_id,
            )
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
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "UPDATE jobs SET result_json = $1::jsonb, cost_usd = $2,"
                " status = $3, updated_at = $4"
                " WHERE job_id = $5 AND workspace_id = $6",
                json.dumps(result),
                cost_usd if cost_usd is not None else 0.0,
                status or "done",
                _now(),
                job_id,
                workspace_id,
            )
        return await self.get(workspace_id, job_id)


class PgUsageStore(_PgBase):
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
        prompt_name: str | None = None,
        prompt_version: int | None = None,
        prompt_sha256: str | None = None,
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
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            created_at=datetime.now(UTC),
        )
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO usage_events (event_id, job_id, workspace_id, role,"
                " model, prompt_tokens, completion_tokens, cost_usd, latency_ms,"
                " prompt_name, prompt_version, prompt_sha256, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
                event.event_id, event.job_id, event.workspace_id, event.role,
                event.model, event.prompt_tokens, event.completion_tokens,
                event.cost_usd, event.latency_ms, event.prompt_name,
                event.prompt_version, event.prompt_sha256,
                event.created_at.isoformat(),
            )
        return event

    async def job_cost(self, job_id: str) -> float:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM usage_events"
                " WHERE job_id = $1",
                job_id,
            )
        return float(row["total"]) if row else 0.0

    async def workspace_summary(self, workspace_id: str) -> UsageSummary:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total_cost,"
                " COALESCE(SUM(prompt_tokens), 0) AS pt,"
                " COALESCE(SUM(completion_tokens), 0) AS ct,"
                " COALESCE(AVG(latency_ms), 0.0) AS avg_lat,"
                " COUNT(*) AS n FROM usage_events WHERE workspace_id = $1",
                workspace_id,
            )
            role_rows = await db.fetch(
                "SELECT role, SUM(cost_usd) AS c FROM usage_events"
                " WHERE workspace_id = $1 GROUP BY role",
                workspace_id,
            )
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

    async def per_role_stats(self, workspace_id: str) -> dict[str, dict[str, float]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT role, SUM(cost_usd) AS c, COUNT(*) AS n,"
                " AVG(latency_ms) AS lat, SUM(prompt_tokens) AS pt,"
                " SUM(completion_tokens) AS ct"
                " FROM usage_events WHERE workspace_id = $1 GROUP BY role",
                workspace_id,
            )
        return {
            r["role"]: {
                "cost_usd": float(r["c"]),
                "calls": int(r["n"]),
                "avg_latency_ms": float(r["lat"] or 0.0),
                "prompt_tokens": int(r["pt"] or 0),
                "completion_tokens": int(r["ct"] or 0),
            }
            for r in rows
        }

    async def prompt_version_mix(
        self, workspace_id: str
    ) -> builtins.list[dict[str, object]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT prompt_name, prompt_version, COUNT(*) AS n"
                " FROM usage_events WHERE workspace_id = $1"
                " AND prompt_name IS NOT NULL"
                " GROUP BY prompt_name, prompt_version"
                " ORDER BY prompt_name, prompt_version",
                workspace_id,
            )
        return [
            {
                "prompt_name": r["prompt_name"],
                "prompt_version": r["prompt_version"],
                "calls": int(r["n"]),
            }
            for r in rows
        ]

    async def daily_cost(
        self, workspace_id: str, *, days: int = 14
    ) -> dict[str, float]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT substr(created_at, 1, 10) AS day,"
                " COALESCE(SUM(cost_usd), 0.0) AS c"
                " FROM usage_events WHERE workspace_id = $1"
                " AND created_at >= $2 GROUP BY day",
                workspace_id, cutoff,
            )
        return {r["day"]: float(r["c"]) for r in rows}


class PgWorkspaceStore(_PgBase):
    async def migrate(self, admin_subject: str | None = None) -> None:
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO workspaces (workspace_id, name, created_at)"
                " VALUES ('default', 'Default workspace', $1)"
                " ON CONFLICT (workspace_id) DO NOTHING",
                _now(),
            )
            if admin_subject:
                await db.execute(
                    "INSERT INTO members (workspace_id, subject, role,"
                    " created_at) VALUES ('default', $1, 'admin', $2)"
                    " ON CONFLICT (workspace_id, subject) DO NOTHING",
                    admin_subject, _now(),
                )

    async def create_workspace(
        self, workspace_id: str, name: str, creator: str | None
    ) -> bool:
        pool = await self._pool()
        async with pool.acquire() as db:
            tag = await db.execute(
                "INSERT INTO workspaces (workspace_id, name, created_at)"
                " VALUES ($1, $2, $3) ON CONFLICT (workspace_id) DO NOTHING",
                workspace_id, name, _now(),
            )
            if _affected(tag) == 0:
                return False
            if creator:
                await db.execute(
                    "INSERT INTO members (workspace_id, subject, role,"
                    " created_at) VALUES ($1, $2, 'admin', $3)"
                    " ON CONFLICT (workspace_id, subject)"
                    " DO UPDATE SET role = EXCLUDED.role",
                    workspace_id, creator, _now(),
                )
            return True

    async def list_workspaces(self, subject: str | None) -> builtins.list[dict[str, str]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT w.workspace_id, w.name, m.role FROM workspaces w"
                " JOIN members m ON m.workspace_id = w.workspace_id"
                " WHERE m.subject = $1 ORDER BY w.workspace_id",
                subject or "",
            )
        return [dict(r) for r in rows]

    async def list_all(self) -> builtins.list[dict[str, str]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT workspace_id, name FROM workspaces ORDER BY workspace_id"
            )
        return [dict(r) for r in rows]

    async def get_member(self, workspace_id: str, subject: str) -> str | None:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT role FROM members WHERE workspace_id = $1 AND subject = $2",
                workspace_id, subject,
            )
        return str(row["role"]) if row else None

    async def add_member(
        self, workspace_id: str, subject: str, role: str
    ) -> None:
        if role not in ROLES:
            msg = f"invalid role: {role}"
            raise ValueError(msg)
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO members (workspace_id, subject, role,"
                " created_at) VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (workspace_id, subject)"
                " DO UPDATE SET role = EXCLUDED.role",
                workspace_id, subject, role, _now(),
            )

    async def set_member_password(
        self, workspace_id: str, subject: str, password_hash: str
    ) -> bool:
        pool = await self._pool()
        async with pool.acquire() as db:
            tag = await db.execute(
                "UPDATE members SET password_hash = $1"
                " WHERE workspace_id = $2 AND subject = $3",
                password_hash, workspace_id, subject,
            )
        return _affected(tag) > 0

    async def get_member_password_hash(
        self, workspace_id: str, subject: str
    ) -> str | None:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT password_hash FROM members"
                " WHERE workspace_id = $1 AND subject = $2",
                workspace_id, subject,
            )
        return str(row["password_hash"]) if row and row["password_hash"] else None

    async def list_members(self, workspace_id: str) -> builtins.list[dict[str, str]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT subject, role, created_at FROM members"
                " WHERE workspace_id = $1 ORDER BY subject",
                workspace_id,
            )
        return [dict(r) for r in rows]

    async def issue_api_key(
        self, workspace_id: str, *, role: str = "member", label: str = ""
    ) -> tuple[str, str]:
        if role not in ROLES:
            msg = f"invalid role: {role}"
            raise ValueError(msg)
        key_id = secrets.token_hex(8)
        full_key = KEY_PREFIX + secrets.token_urlsafe(24)
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO api_keys (key_id, workspace_id, key_hash, prefix,"
                " role, label, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                key_id, workspace_id, hash_api_key(full_key),
                full_key[: len(KEY_PREFIX) + 6], role, label, _now(),
            )
        return key_id, full_key

    async def lookup_api_key(self, key: str) -> ApiKeyRecord | None:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT key_id, workspace_id, role, label FROM api_keys"
                " WHERE key_hash = $1 AND revoked = 0",
                hash_api_key(key),
            )
        if row is None:
            return None
        return ApiKeyRecord(
            key_id=row["key_id"],
            workspace_id=row["workspace_id"],
            role=row["role"],
            label=row["label"],
        )

    async def list_api_keys(self, workspace_id: str) -> builtins.list[dict[str, str]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT key_id, prefix, role, label, created_at, revoked"
                " FROM api_keys WHERE workspace_id = $1 ORDER BY created_at",
                workspace_id,
            )
        return [dict(r) for r in rows]

    async def revoke_api_key(self, workspace_id: str, key_id: str) -> bool:
        pool = await self._pool()
        async with pool.acquire() as db:
            tag = await db.execute(
                "UPDATE api_keys SET revoked = 1"
                " WHERE workspace_id = $1 AND key_id = $2",
                workspace_id, key_id,
            )
        return _affected(tag) > 0


class PgFeedbackStore(_PgBase):
    async def record(
        self,
        workspace_id: str,
        job_id: str,
        rating: str,
        comment: str = "",
        section: str | None = None,
    ) -> int:
        if rating not in ("up", "down"):
            raise ValueError("rating must be 'up' or 'down'")
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "INSERT INTO feedback"
                " (workspace_id, job_id, section, rating, comment, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                workspace_id, job_id, section, rating, comment, time.time(),
            )
        return int(row["id"]) if row else 0

    async def list(
        self, workspace_id: str, *, limit: int = 100
    ) -> builtins.list[dict[str, Any]]:
        pool = await self._pool()
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT * FROM feedback WHERE workspace_id = $1"
                " ORDER BY created_at DESC LIMIT $2",
                workspace_id, limit,
            )
        return [dict(r) for r in rows]

    async def export_cases(
        self, workspace_id: str, *, limit: int = 500
    ) -> builtins.list[dict[str, Any]]:
        rows = await self.list(workspace_id, limit=limit)
        return [
            {
                "source": "operator-feedback",
                "job_id": r["job_id"],
                "workspace_id": r["workspace_id"],
                "section": r["section"],
                "rating": r["rating"],
                "comment": r["comment"],
                "created_at": r["created_at"],
                "candidate_case": {
                    "id": f"fb-{r['id']}",
                    "note": "reviewed manually — not part of the gated dataset",
                },
            }
            for r in rows
        ]

    async def close(self) -> None:
        # The pool is shared across all Pg stores — it belongs to the
        # PostgresStores facade, not to one store; kept for protocol
        # symmetry with the SQLite no-op.
        return None


class PgTokenStore(_PgBase):
    def _iso(self, dt: datetime) -> str:
        return dt.astimezone(UTC).isoformat()

    async def revoke(self, jti: str, expires_at: datetime) -> None:
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO revoked_tokens (jti, expires_at) VALUES ($1, $2)"
                " ON CONFLICT (jti) DO NOTHING",
                jti, self._iso(expires_at),
            )

    async def is_revoked(self, jti: str) -> bool:
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT 1 AS x FROM revoked_tokens"
                " WHERE jti = $1 AND expires_at > $2",
                jti, self._iso(datetime.now(UTC)),
            )
        return row is not None

    async def sweep(self, now: datetime | None = None) -> int:
        pool = await self._pool()
        async with pool.acquire() as db:
            tag = await db.execute(
                "DELETE FROM revoked_tokens WHERE expires_at <= $1",
                self._iso(now or datetime.now(UTC)),
            )
        return _affected(tag)


class PgCacheStore(_PgBase):
    def _day(self, now: datetime) -> str:
        return now.astimezone(UTC).date().isoformat()

    async def get(
        self,
        kind: str,
        workspace_id: str,
        key: str,
        *,
        ttl_hours: float,
        now: datetime | None = None,
    ) -> str | None:
        moment = now or datetime.now(UTC)
        pool = await self._pool()
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT payload_json, fetched_at FROM fetch_cache"
                " WHERE kind = $1 AND workspace_id = $2 AND cache_key = $3"
                " AND day = $4",
                kind, workspace_id, key, self._day(moment),
            )
            if row is None:
                return None
            fetched = datetime.fromisoformat(row["fetched_at"])
            if moment - fetched > timedelta(hours=ttl_hours):
                await db.execute(
                    "DELETE FROM fetch_cache WHERE kind = $1"
                    " AND workspace_id = $2 AND cache_key = $3 AND day = $4",
                    kind, workspace_id, key, self._day(moment),
                )
                return None
            return str(row["payload_json"])

    async def put(
        self,
        kind: str,
        workspace_id: str,
        key: str,
        payload_json: str,
        *,
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now(UTC)
        pool = await self._pool()
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO fetch_cache"
                " (kind, workspace_id, cache_key, day, payload_json, fetched_at)"
                " VALUES ($1, $2, $3, $4, $5, $6)"
                " ON CONFLICT (kind, workspace_id, cache_key, day)"
                " DO UPDATE SET payload_json = EXCLUDED.payload_json,"
                " fetched_at = EXCLUDED.fetched_at",
                kind, workspace_id, key, self._day(moment),
                payload_json, moment.isoformat(),
            )

    async def prune(self, *, now: datetime | None = None) -> int:
        today = self._day(now or datetime.now(UTC))
        pool = await self._pool()
        async with pool.acquire() as db:
            tag = await db.execute(
                "DELETE FROM fetch_cache WHERE day < $1", today
            )
        return _affected(tag)


class PostgresStores:
    """Facade: all six stores over one lazily-created pool."""

    def __init__(self, dsn: str) -> None:
        self._holder = _PoolHolder(dsn)
        self.job_store = PgJobStore(self._holder)
        self.usage_store = PgUsageStore(self._holder)
        self.workspace_store = PgWorkspaceStore(self._holder)
        self.feedback_store = PgFeedbackStore(self._holder)
        self.token_store = PgTokenStore(self._holder)
        self.cache_store = PgCacheStore(self._holder)

    async def close(self) -> None:
        await self._holder.close()
