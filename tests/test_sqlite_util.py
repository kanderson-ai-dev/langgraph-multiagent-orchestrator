"""Fresh-clone safety: connection helpers create their own parent folder.

Regression tests for the bare-clone failure where ``data/`` did not exist
and every store that relied on ``open_connection``/``connect`` raised
``sqlite3.OperationalError: unable to open database file``.
"""

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.graph.state import Brief
from app.services.cache_store import CacheStore
from app.services.feedback_store import FeedbackStore
from app.services.job_store import JobStore
from app.services.sqlite_util import connect, connect_sync, open_connection
from app.services.token_store import SqliteTokenStore
from app.services.usage_store import UsageStore
from app.services.workspace_store import WorkspaceStore

_NESTED = ("does", "not", "exist")


def _nested(tmp_path: Any) -> str:
    return str(Path(tmp_path, *_NESTED) / "x.sqlite")


async def test_open_connection_creates_parent_dirs(tmp_path: Any) -> None:
    db_path = _nested(tmp_path)
    db = await open_connection(db_path)
    try:
        await db.execute("SELECT 1")
    finally:
        await db.close()
    assert Path(db_path).exists()


async def test_connect_creates_parent_dirs(tmp_path: Any) -> None:
    db_path = _nested(tmp_path)
    async with connect(db_path) as db:
        await db.execute("SELECT 1")
    assert Path(db_path).exists()


def test_connect_sync_creates_parent_dirs(tmp_path: Any) -> None:
    db_path = _nested(tmp_path)
    conn = connect_sync(db_path)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    assert Path(db_path).exists()


async def test_connect_relative_path_creates_dirs(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    async with connect(os.path.join("rel", "nested", "x.sqlite")) as db:
        await db.execute("SELECT 1")
    assert (tmp_path / "rel" / "nested" / "x.sqlite").exists()


async def test_memory_database_still_works() -> None:
    async with connect(":memory:") as db:
        await db.execute("SELECT 1")


def test_connect_sync_memory_database_still_works() -> None:
    conn = connect_sync(":memory:")
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


async def test_parent_is_file_raises_clear_error(tmp_path: Any) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    db_path = str(blocker / "x.sqlite")
    with pytest.raises(OSError, match="afile"):
        await open_connection(db_path)


@pytest.mark.skipif(os.name == "nt", reason="Windows ignores dir read-only bit")
async def test_readonly_parent_raises_readable_error(tmp_path: Any) -> None:
    ro = tmp_path / "readonly"
    ro.mkdir()
    ro.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        with pytest.raises(OSError, match="x.sqlite|readonly|cannot"):
            await open_connection(str(ro / "x.sqlite"))
    finally:
        ro.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


async def test_job_store_creates_parent_dirs(tmp_path: Any) -> None:
    store = JobStore(_nested(tmp_path))
    rec = await store.create("w", Brief(topic="topic"))
    assert rec.job_id


async def test_workspace_store_creates_parent_dirs(tmp_path: Any) -> None:
    store = WorkspaceStore(_nested(tmp_path))
    await store.migrate()
    assert await store.list_all()


async def test_token_store_creates_parent_dirs(tmp_path: Any) -> None:
    store = SqliteTokenStore(_nested(tmp_path))
    await store.revoke("jti-1", datetime.now(UTC) + timedelta(hours=1))
    assert await store.is_revoked("jti-1")


async def test_feedback_store_creates_parent_dirs(tmp_path: Any) -> None:
    store = FeedbackStore(_nested(tmp_path))
    row_id = await store.record("w", "job-1", "up")
    assert row_id > 0


async def test_usage_store_creates_parent_dirs(tmp_path: Any) -> None:
    store = UsageStore(_nested(tmp_path))
    await store.record(
        job_id="j1", workspace_id="w", role="writer", model="stub", cost_usd=0.001
    )
    assert (await store.job_cost("j1")) > 0


async def test_cache_store_creates_parent_dirs(tmp_path: Any) -> None:
    store = CacheStore(_nested(tmp_path))
    await store.put("search", "w", "k", "{}")
    assert await store.get("search", "w", "k", ttl_hours=1.0) == "{}"
