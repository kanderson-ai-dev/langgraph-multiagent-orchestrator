#!/usr/bin/env python3
"""Apply the Postgres store schema (idempotent) to a DATABASE_URL.

The stores auto-migrate on first use, so this is only needed when you
want the tables in place before the app starts — e.g. a least-privilege
role that may read/write but not ``CREATE TABLE``.

Usage:
    uv run python scripts/pg_migrate.py "$DATABASE_URL"
    uv run python scripts/pg_migrate.py --check "$DATABASE_URL"  # report, no changes
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPECTED_TABLES = {
    "jobs",
    "usage_events",
    "workspaces",
    "members",
    "api_keys",
    "feedback",
    "revoked_tokens",
    "fetch_cache",
}


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    return {r["tablename"] for r in rows}


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv
    if not args:
        print(__doc__)
        return 2
    dsn = args[0]

    from app.services.pg.stores import SCHEMA

    conn = await asyncpg.connect(dsn)
    try:
        before = await _tables(conn)
        if check:
            missing = sorted(EXPECTED_TABLES - before)
            if missing:
                print(f"missing tables: {', '.join(missing)}")
                return 1
            print("schema up to date")
            return 0
        await conn.execute(SCHEMA)
        after = await _tables(conn)
        created = sorted(after - before)
        print(f"applied schema ({len(created)} created: {', '.join(created) or 'none'})")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
