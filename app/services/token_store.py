"""JWT revocation store — server-side logout for stateless tokens.

JWTs are self-contained: once issued they verify until ``exp``. A
``revoked_tokens`` table holding token ids (``jti``) until their natural
expiry gives sign-out real semantics — a revoked token stops working
immediately instead of surviving until expiry. Rows are swept once they
could no longer verify anyway, so the table stays small.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class TokenStore(Protocol):
    """Revoked-token record — SQLite today, Redis for multi-replica later."""

    async def revoke(self, jti: str, expires_at: datetime) -> None:
        """Mark ``jti`` revoked until ``expires_at`` (its own expiry)."""
        ...

    async def is_revoked(self, jti: str) -> bool:
        ...

    async def sweep(self, now: datetime | None = None) -> int:
        """Drop rows whose tokens expired anyway; returns rows removed."""
        ...


class SqliteTokenStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    async def _db(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self._db_path)
        db.row_factory = sqlite3.Row
        if not self._initialized:
            await db.executescript(_SCHEMA)
            await db.commit()
            self._initialized = True
        return db

    async def revoke(self, jti: str, expires_at: datetime) -> None:
        db = await self._db()
        try:
            await db.execute(
                "INSERT OR IGNORE INTO revoked_tokens (jti, expires_at)"
                " VALUES (?, ?)",
                (jti, _iso(expires_at)),
            )
            await db.commit()
        finally:
            await db.close()

    async def is_revoked(self, jti: str) -> bool:
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT 1 FROM revoked_tokens WHERE jti = ? AND expires_at > ?",
                (jti, _iso(datetime.now(UTC))),
            )
            return await cur.fetchone() is not None
        finally:
            await db.close()

    async def sweep(self, now: datetime | None = None) -> int:
        db = await self._db()
        try:
            cur = await db.execute(
                "DELETE FROM revoked_tokens WHERE expires_at <= ?",
                (_iso(now or datetime.now(UTC)),),
            )
            await db.commit()
            return cur.rowcount
        finally:
            await db.close()
