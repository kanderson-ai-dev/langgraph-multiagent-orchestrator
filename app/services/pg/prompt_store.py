"""Synchronous Postgres prompt-registry store (``psycopg``).

The registry is synchronous (startup, routes, evaluation), so it cannot
reuse the ``asyncpg`` stores — this is the psycopg-backed implementation
of the same ``PromptStoreProtocol``. Verified against a live backend by
``tests/test_store_contract.py`` when ``TEST_DATABASE_URL`` is set.
"""
from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from app.services.prompt_store import _EVENT_COLS, _SCHEMA_PG, _VERSION_COLS

if TYPE_CHECKING:
    from app.services.prompt_registry import PromptVersion


class PgPromptStore:
    """``psycopg``-backed store — lazy connect, one lock serializes calls
    (a psycopg Connection is not thread-safe)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None
        self._lock = threading.Lock()

    def _db(self) -> Any:
        if self._conn is None:
            import psycopg
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(
                self._dsn, row_factory=dict_row, autocommit=False
            )
        return self._conn

    def migrate(self) -> None:
        with self._lock:
            conn = self._db()
            conn.execute(_SCHEMA_PG)
            # Same additive migration as SQLite: older DBs lack `sig`.
            conn.execute(
                "ALTER TABLE prompt_events"
                " ADD COLUMN IF NOT EXISTS sig TEXT NOT NULL DEFAULT ''"
            )
            conn.commit()

    def version_row(self, name: str, version: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute(
                "SELECT sha256, source FROM prompt_versions"
                " WHERE name=%s AND version=%s",
                (name, version),
            ).fetchone()
        return dict(row) if row else None

    def upsert_version(
        self,
        pv: PromptVersion,
        *,
        source: str,
        status_overridden: int,
        preserve_overridden_status: bool,
    ) -> None:
        status_clause = (
            "   status=CASE WHEN prompt_versions.status_overridden=1"
            "     THEN prompt_versions.status ELSE excluded.status END,"
            if preserve_overridden_status
            else "   status=excluded.status,"
        )
        with self._lock:
            conn = self._db()
            conn.execute(
                f"INSERT INTO prompt_versions ({_VERSION_COLS})"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(name, version) DO UPDATE SET"
                "   author=excluded.author, created_at=excluded.created_at,"
                "   target_model=excluded.target_model,"
                "   variables=excluded.variables, changelog=excluded.changelog,"
                "   template=excluded.template, sha256=excluded.sha256,"
                f"   source=excluded.source, {status_clause}"
                "   status_overridden=excluded.status_overridden",
                (
                    pv.name, pv.version, pv.status.value, pv.author,
                    pv.created_at.isoformat(), pv.target_model,
                    json.dumps(pv.variables), pv.changelog,
                    pv.template, pv.sha256, source, status_overridden,
                ),
            )
            conn.commit()

    def fetch_versions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM prompt_versions ORDER BY name, version"
            ).fetchall()
        return [dict(r) for r in rows]

    def revision(self) -> int:
        with self._lock:
            row = self._db().execute(
                "SELECT MAX(seq) AS s FROM prompt_events"
            ).fetchone()
        return int(row["s"] or 0)

    def last_event_hash(self) -> str | None:
        with self._lock:
            row = self._db().execute(
                "SELECT hash FROM prompt_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return str(row["hash"]) if row else None

    def record_event(self, values: dict[str, Any]) -> None:
        with self._lock:
            conn = self._db()
            conn.execute(
                f"INSERT INTO prompt_events ({_EVENT_COLS})"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(values[c] for c in _EVENT_COLS.split(", ")),
            )
            conn.commit()

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM prompt_events ORDER BY seq"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_ab_run(
        self,
        run_id: str,
        *,
        name: str,
        version: int,
        workspace_id: str,
        status: str,
        result_json: str,
        created_at: str,
    ) -> None:
        with self._lock:
            conn = self._db()
            conn.execute(
                "INSERT INTO prompt_ab_runs"
                " (run_id, name, version, workspace_id, status, result_json,"
                "  created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                "   status=excluded.status, result_json=excluded.result_json",
                (run_id, name, version, workspace_id, status,
                 result_json, created_at),
            )
            conn.commit()

    def get_ab_run(
        self, run_id: str, workspace_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute(
                "SELECT * FROM prompt_ab_runs"
                " WHERE run_id = %s AND workspace_id = %s",
                (run_id, workspace_id),
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

