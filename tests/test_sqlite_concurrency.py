"""SQLite concurrency: shared pragmas (WAL, busy timeout) actually applied.

Concurrent jobs and the HITL sweeper all write to one database file; this
file proves the shared connection helper applies the pragmas that keep
readers/writers from failing with ``database is locked``.
"""

import asyncio
from typing import Any

from app.graph.state import Brief
from app.services.job_store import JobStore
from app.services.sqlite_util import connect
from app.services.usage_store import UsageStore


async def test_shared_pragmas_applied(tmp_path: Any) -> None:
    async with connect(str(tmp_path / "x.sqlite")) as db:
        cur = await db.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0] == "wal"
        cur = await db.execute("PRAGMA busy_timeout")
        assert (await cur.fetchone())[0] == 5000
        cur = await db.execute("PRAGMA foreign_keys")
        assert (await cur.fetchone())[0] == 1
        cur = await db.execute("PRAGMA synchronous")
        assert (await cur.fetchone())[0] == 1  # NORMAL


async def test_memory_database_is_safe() -> None:
    # journal_mode is a no-op on :memory: — the helper must not break tests
    # that use in-memory databases.
    async with connect(":memory:") as db:
        cur = await db.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0] == "memory"


async def test_concurrent_writers_never_raise_locked(tmp_path: Any) -> None:
    """25 interleaved writers on one file must all complete."""
    db = str(tmp_path / "jobs.sqlite")
    store, usage = JobStore(db), UsageStore(db)
    rec = await store.create("w", Brief(topic="concurrency probe"))

    async def writer(i: int) -> None:
        await store.update_status("w", rec.job_id, "running")
        await usage.record(
            job_id=rec.job_id,
            workspace_id="w",
            role="writer",
            model="stub",
            cost_usd=0.001,
        )

    results = await asyncio.gather(
        *(writer(i) for i in range(25)), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"concurrent writes failed: {errors[:3]}"
