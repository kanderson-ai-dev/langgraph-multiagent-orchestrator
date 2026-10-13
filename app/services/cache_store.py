"""SQLite fetch cache — per-tenant, day-scoped, TTL-bounded.

Keys are ``(kind, workspace_id, key, day)`` where ``kind`` is ``scrape`` or
``search`` and ``key`` is the URL / normalized query. Tenant scoping is
structural: a cached page is never served to a different workspace, which
is also what keeps tenant-specific content from leaking across tenants.

Reads enforce the TTL (``cache_ttl_hours``); stale rows are deleted on
read-miss so the table self-prunes.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

_SCHEMA = """
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


def _day(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


class CacheStore:
    """Async fetch cache sharing the app SQLite database file."""

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

    async def get(
        self,
        kind: str,
        workspace_id: str,
        key: str,
        *,
        ttl_hours: float,
        now: datetime | None = None,
    ) -> str | None:
        """Return the cached payload, or ``None`` on miss/stale.

        Same-day keying plus TTL: a row is only a hit when its ``day``
        matches ``now``'s UTC day *and* it is younger than the TTL.
        """
        await self._ensure_schema()
        moment = now or datetime.now(UTC)
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT payload_json, fetched_at FROM fetch_cache"
                " WHERE kind = ? AND workspace_id = ? AND cache_key = ?"
                " AND day = ?",
                (kind, workspace_id, key, _day(moment)),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            fetched = datetime.fromisoformat(row[1])
            if moment - fetched > timedelta(hours=ttl_hours):
                await db.execute(
                    "DELETE FROM fetch_cache WHERE kind = ?"
                    " AND workspace_id = ? AND cache_key = ? AND day = ?",
                    (kind, workspace_id, key, _day(moment)),
                )
                await db.commit()
                return None
            return str(row[0])

    async def put(
        self,
        kind: str,
        workspace_id: str,
        key: str,
        payload_json: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Store a payload under today's key (upsert — retries are safe)."""
        await self._ensure_schema()
        moment = now or datetime.now(UTC)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO fetch_cache"
                " (kind, workspace_id, cache_key, day, payload_json,"
                "  fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    kind, workspace_id, key, _day(moment),
                    payload_json, moment.isoformat(),
                ),
            )
            await db.commit()

    async def prune(self, *, now: datetime | None = None) -> int:
        """Drop rows from previous UTC days — explicit cleanup helper."""
        await self._ensure_schema()
        today = _day(now or datetime.now(UTC))
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM fetch_cache WHERE day < ?", (today,)
            )
            await db.commit()
            return int(cursor.rowcount or 0)
