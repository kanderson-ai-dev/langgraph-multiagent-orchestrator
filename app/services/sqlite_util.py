"""Shared SQLite connection setup — WAL, busy timeout, foreign keys.

Every store in the app opens short-lived connections against the same
file. Without tuning, a reader hitting a write transaction fails
immediately with ``OperationalError: database is locked``. The pragmas:

- ``journal_mode=WAL`` — readers never block on an uncommitted writer.
- ``busy_timeout=5000`` — writers queue up to 5 s instead of failing
  instantly; SQLite remains single-writer regardless.
- ``synchronous=NORMAL`` — safe in WAL; only a full OS/power loss can
  roll back the last commit (trade-off documented in ADR 0003).
- ``foreign_keys=ON`` — off by default in SQLite; enforcement is free.

``journal_mode`` is a no-op on ``:memory:`` (returns ``memory``), so the
helpers are safe for the temp/in-memory databases tests use.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiosqlite

# busy_timeout first: every later statement (including the WAL switch)
# waits up to 5 s instead of failing instantly. journal_mode is applied
# conditionally — switching DELETE→WAL races active connections
# (SQLITE_BUSY_SNAPSHOT is *not* retried by the busy handler), so the
# steady-state path does a cheap SELECT and never takes the lock.
_PRAGMAS_TAIL = (
    "PRAGMA synchronous=NORMAL;"
    "PRAGMA foreign_keys=ON;"
)


def _ensure_parent(path: str) -> None:
    """Create the database file's parent directory when there is one.

    Skipped for ``:memory:`` and ``file:`` URIs — the parent of a URI is
    not a filesystem directory. Every store goes through this helper, so
    a bare checkout without ``data/`` works without each caller mkdir-ing
    itself. Failure raises a readable error naming the database path,
    not a bare ``unable to open database file``.
    """
    if path == ":memory:" or path.startswith("file:"):
        return
    parent = Path(path).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"cannot create parent directory for SQLite database {path!r}: {exc}"
        ) from exc
    if not os.access(parent, os.W_OK | os.X_OK):
        raise OSError(f"parent directory for SQLite database {path!r} is not writable")


async def _apply_pragmas(db: Any) -> None:
    await db.execute("PRAGMA busy_timeout=5000")
    cur = await db.execute("PRAGMA journal_mode")
    if (await cur.fetchone())[0] != "wal":
        await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_PRAGMAS_TAIL)


async def open_connection(path: str) -> aiosqlite.Connection:
    """Open an aiosqlite connection with the shared pragmas applied.

    The caller owns the returned connection and must close it.
    """
    _ensure_parent(path)
    # ``connect()`` spawns the worker thread synchronously; split the
    # await so a cancellation lands inside the try where close() runs.
    db = aiosqlite.connect(path)
    try:
        await db
        for attempt in range(6):
            try:
                await _apply_pragmas(db)
                return db
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 5:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        return db  # unreachable; keeps the return type obvious
    except BaseException:
        # Cancellation (e.g. runner shutdown mid-open) must not strand a
        # live worker thread — close before propagating.
        with suppress(Exception):
            await db.close()
        raise


def _apply_pragmas_sync(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_PRAGMAS_TAIL)


@asynccontextmanager
async def connect(path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Context-managed variant of :func:`open_connection` (auto-close)."""
    _ensure_parent(path)
    db = await open_connection(path)
    try:
        yield db
    finally:
        await db.close()


def connect_sync(path: str, **kwargs: Any) -> sqlite3.Connection:
    """Synchronous twin for stores that keep one long-lived connection."""
    _ensure_parent(path)
    conn: sqlite3.Connection = sqlite3.connect(path, **kwargs)
    for attempt in range(6):
        try:
            _apply_pragmas_sync(conn)
            return conn
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 5:
                conn.close()
                raise
            time.sleep(0.05 * (attempt + 1))
    return conn  # unreachable; keeps the return type obvious
