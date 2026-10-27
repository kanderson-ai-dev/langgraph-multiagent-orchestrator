"""Prompt-registry persistence — the store protocol behind the overlay.

The registry itself is synchronous, so this seam is too. One semantic
operation per thing the registry needs — the SQL dialect lives inside
each implementation, not in the registry.

- ``SqlitePromptStore`` — the historical ``connect_sync`` behavior:
  long-lived connection, schema auto-migration on open, serialized by an
  internal lock (``check_same_thread=False`` alone is not a thread-safety
  guarantee).
- ``PgPromptStore`` — same contract over ``psycopg`` (sync driver; the
  registry can't await). Lives in ``app/services/pg/prompt_store.py``
  alongside the other Postgres driver code.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.services.prompt_registry import PromptVersion

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL,
    status            TEXT NOT NULL,
    author            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    target_model      TEXT NOT NULL DEFAULT '',
    variables         TEXT NOT NULL DEFAULT '[]',
    changelog         TEXT NOT NULL DEFAULT '',
    template          TEXT NOT NULL,
    sha256            TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'yaml',
    status_overridden INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS prompt_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    sig          TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_ab_runs (
    run_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    result_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
"""

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL,
    status            TEXT NOT NULL,
    author            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    target_model      TEXT NOT NULL DEFAULT '',
    variables         TEXT NOT NULL DEFAULT '[]',
    changelog         TEXT NOT NULL DEFAULT '',
    template          TEXT NOT NULL,
    sha256            TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'yaml',
    status_overridden INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS prompt_events (
    seq          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    sig          TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_ab_runs (
    run_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    result_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
"""

_VERSION_COLS = (
    "name, version, status, author, created_at, target_model, variables,"
    " changelog, template, sha256, source, status_overridden"
)

_EVENT_COLS = (
    "name, version, action, actor, reason, metrics_json, prev_hash, hash,"
    " sig, created_at"
)


class PromptStoreProtocol(Protocol):
    """Synchronous persistence for the prompt registry overlay."""

    def migrate(self) -> None:
        """Idempotent schema setup (CREATE TABLE IF NOT EXISTS + adds)."""
        ...

    def version_row(self, name: str, version: int) -> dict[str, Any] | None:
        ...

    def upsert_version(
        self,
        pv: PromptVersion,
        *,
        source: str,
        status_overridden: int,
        preserve_overridden_status: bool,
    ) -> None:
        """Insert-or-update one ``prompt_versions`` row.

        ``preserve_overridden_status`` keeps a runtime status override
        over a re-synced YAML seed (the seed's status wins otherwise).
        """
        ...

    def fetch_versions(self) -> list[dict[str, Any]]:
        ...

    def revision(self) -> int:
        """MAX(seq) of ``prompt_events`` — the freshness watermark."""
        ...

    def last_event_hash(self) -> str | None:
        ...

    def record_event(self, values: dict[str, Any]) -> None:
        """Append one ``prompt_events`` row (keys = ``_EVENT_COLS``)."""
        ...

    def events(self) -> list[dict[str, Any]]:
        ...

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
        ...

    def get_ab_run(
        self, run_id: str, workspace_id: str
    ) -> dict[str, Any] | None:
        ...

    def close(self) -> None:
        ...


class SqlitePromptStore:
    """``connect_sync``-backed store — the historical overlay."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            from app.services.sqlite_util import connect_sync

            conn = connect_sync(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._conn = conn
            # Finalizer keeps the conn alive past the store's own GC and
            # closes it there — explicit close() stays the eager path, so
            # a forgotten store never leaves an unclosed database behind.
            weakref.finalize(self, conn.close)
        return self._conn

    def migrate(self) -> None:
        with self._lock:
            conn = self._db()
            conn.executescript(_SCHEMA_SQLITE)
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(prompt_events)")
            }
            if "sig" not in cols:
                conn.execute(
                    "ALTER TABLE prompt_events ADD COLUMN sig TEXT"
                    " NOT NULL DEFAULT ''"
                )
            conn.commit()

    def version_row(self, name: str, version: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute(
                "SELECT sha256, source FROM prompt_versions"
                " WHERE name=? AND version=?",
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
        if preserve_overridden_status:
            # A runtime status override (promote/retire/rollback) must
            # survive every later seed re-sync — preserving the status but
            # clearing the flag made the override last exactly one sync.
            status_fragment = (
                "   status=CASE WHEN prompt_versions.status_overridden=1"
                "     THEN prompt_versions.status ELSE excluded.status END,"
                "   status_overridden=CASE WHEN prompt_versions.status_overridden=1"
                "     THEN 1 ELSE excluded.status_overridden END"
            )
        else:
            status_fragment = (
                "   status=excluded.status,"
                "   status_overridden=excluded.status_overridden"
            )
        with self._lock:
            conn = self._db()
            conn.execute(
                f"INSERT INTO prompt_versions ({_VERSION_COLS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(name, version) DO UPDATE SET"
                "   author=excluded.author, created_at=excluded.created_at,"
                "   target_model=excluded.target_model,"
                "   variables=excluded.variables, changelog=excluded.changelog,"
                "   template=excluded.template, sha256=excluded.sha256,"
                f"   source=excluded.source, {status_fragment}",
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
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                "  created_at) VALUES (?,?,?,?,?,?,?)"
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
                " WHERE run_id = ? AND workspace_id = ?",
                (run_id, workspace_id),
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
