"""Multi-tenant workspace administration — workspaces, members, API keys.

Roles are ``viewer`` < ``member`` < ``admin`` (see
``app.core.dependencies.ROLE_RANK``). API keys are shown exactly once at
issuance; only the SHA-256 hash and a short display prefix persist.
"""

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from app.services.sqlite_util import open_connection

_SCHEMA = """
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
"""

# Columns added after the initial release — ALTER TABLE migrations guarded
# by PRAGMA table_info (same convention as usage_store).
_MIGRATIONS = (
    "ALTER TABLE members ADD COLUMN password_hash TEXT",
)

ROLES = ("viewer", "member", "admin")
KEY_PREFIX = "orx_"


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    workspace_id: str
    role: str
    label: str


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class WorkspaceStore:
    """SQLite-backed workspace/member/API-key store."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    async def _db(self) -> aiosqlite.Connection:
        db = await open_connection(self._db_path)
        db.row_factory = sqlite3.Row
        if not self._initialized:
            await db.executescript(_SCHEMA)
            cursor = await db.execute("PRAGMA table_info(members)")
            existing = {row[1] for row in await cursor.fetchall()}
            for stmt in _MIGRATIONS:
                column = stmt.rsplit(" ", 2)[-2]
                if column not in existing:
                    await db.execute(stmt)
            await db.commit()
            self._initialized = True
        return db

    # ─── bootstrap / migration ──────────────────────────────────────────

    async def migrate(self, admin_subject: str | None = None) -> None:
        """Idempotent bootstrap: ensure the implicit ``default`` tenant and
        the platform admin's membership exist for pre-RBAC deployments."""
        db = await self._db()
        try:
            await db.execute(
                "INSERT OR IGNORE INTO workspaces (workspace_id, name, created_at)"
                " VALUES ('default', 'Default workspace', ?)",
                (_now(),),
            )
            if admin_subject:
                await db.execute(
                    "INSERT OR IGNORE INTO members (workspace_id, subject, role,"
                    " created_at) VALUES ('default', ?, 'admin', ?)",
                    (admin_subject, _now()),
                )
            await db.commit()
        finally:
            await db.close()

    # ─── workspaces ─────────────────────────────────────────────────────

    async def create_workspace(
        self, workspace_id: str, name: str, creator: str | None
    ) -> bool:
        """Create a workspace and make ``creator`` its admin member.

        Returns False when the id is already taken.
        """
        db = await self._db()
        try:
            cur = await db.execute(
                "INSERT OR IGNORE INTO workspaces (workspace_id, name, created_at)"
                " VALUES (?, ?, ?)",
                (workspace_id, name, _now()),
            )
            if cur.rowcount == 0:
                return False
            if creator:
                await db.execute(
                    "INSERT OR REPLACE INTO members (workspace_id, subject, role,"
                    " created_at) VALUES (?, ?, 'admin', ?)",
                    (workspace_id, creator, _now()),
                )
            await db.commit()
            return True
        finally:
            await db.close()

    async def list_workspaces(self, subject: str | None) -> list[dict[str, str]]:
        """Workspaces where ``subject`` holds a membership (with the role)."""
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT w.workspace_id, w.name, m.role FROM workspaces w"
                " JOIN members m ON m.workspace_id = w.workspace_id"
                " WHERE m.subject = ? ORDER BY w.workspace_id",
                (subject or "",),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def list_all(self) -> list[dict[str, str]]:
        """Every workspace (dev-mode listing only — no role column)."""
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT workspace_id, name FROM workspaces ORDER BY workspace_id"
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    # ─── members ────────────────────────────────────────────────────────

    async def get_member(self, workspace_id: str, subject: str) -> str | None:
        """The subject's role in the workspace, or None when not a member."""
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT role FROM members WHERE workspace_id = ? AND subject = ?",
                (workspace_id, subject),
            )
            row = await cur.fetchone()
            return str(row["role"]) if row else None
        finally:
            await db.close()

    async def add_member(
        self, workspace_id: str, subject: str, role: str
    ) -> None:
        if role not in ROLES:
            msg = f"invalid role: {role}"
            raise ValueError(msg)
        db = await self._db()
        try:
            await db.execute(
                "INSERT INTO members (workspace_id, subject, role,"
                " created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(workspace_id, subject)"
                " DO UPDATE SET role = excluded.role",
                (workspace_id, subject, role, _now()),
            )
            await db.commit()
        finally:
            await db.close()

    async def set_member_password(
        self, workspace_id: str, subject: str, password_hash: str
    ) -> bool:
        """Attach/rotate a member's login password. False if no member row."""
        db = await self._db()
        try:
            cur = await db.execute(
                "UPDATE members SET password_hash = ?"
                " WHERE workspace_id = ? AND subject = ?",
                (password_hash, workspace_id, subject),
            )
            await db.commit()
            return cur.rowcount > 0
        finally:
            await db.close()

    async def get_member_password_hash(
        self, workspace_id: str, subject: str
    ) -> str | None:
        """The member's password hash for login, or None when unset/absent."""
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT password_hash FROM members"
                " WHERE workspace_id = ? AND subject = ?",
                (workspace_id, subject),
            )
            row = await cur.fetchone()
            return str(row["password_hash"]) if row and row["password_hash"] else None
        finally:
            await db.close()

    async def list_members(self, workspace_id: str) -> list[dict[str, str]]:
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT subject, role, created_at FROM members"
                " WHERE workspace_id = ? ORDER BY subject",
                (workspace_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    # ─── API keys ───────────────────────────────────────────────────────

    async def issue_api_key(
        self,
        workspace_id: str,
        *,
        role: str = "member",
        label: str = "",
    ) -> tuple[str, str]:
        """Mint ``orx_…`` key; returns ``(key_id, full_key)`` — shown once."""
        if role not in ROLES:
            msg = f"invalid role: {role}"
            raise ValueError(msg)
        key_id = secrets.token_hex(8)
        full_key = KEY_PREFIX + secrets.token_urlsafe(24)
        db = await self._db()
        try:
            await db.execute(
                "INSERT INTO api_keys (key_id, workspace_id, key_hash, prefix,"
                " role, label, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    workspace_id,
                    hash_api_key(full_key),
                    full_key[: len(KEY_PREFIX) + 6],
                    role,
                    label,
                    _now(),
                ),
            )
            await db.commit()
        finally:
            await db.close()
        return key_id, full_key

    async def lookup_api_key(self, key: str) -> ApiKeyRecord | None:
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT key_id, workspace_id, role, label FROM api_keys"
                " WHERE key_hash = ? AND revoked = 0",
                (hash_api_key(key),),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return ApiKeyRecord(
                key_id=row["key_id"],
                workspace_id=row["workspace_id"],
                role=row["role"],
                label=row["label"],
            )
        finally:
            await db.close()

    async def list_api_keys(self, workspace_id: str) -> list[dict[str, str]]:
        """Metadata only — never the key or its hash."""
        db = await self._db()
        try:
            cur = await db.execute(
                "SELECT key_id, prefix, role, label, created_at, revoked"
                " FROM api_keys WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def revoke_api_key(self, workspace_id: str, key_id: str) -> bool:
        db = await self._db()
        try:
            cur = await db.execute(
                "UPDATE api_keys SET revoked = 1"
                " WHERE workspace_id = ? AND key_id = ?",
                (workspace_id, key_id),
            )
            await db.commit()
            return cur.rowcount > 0
        finally:
            await db.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()
