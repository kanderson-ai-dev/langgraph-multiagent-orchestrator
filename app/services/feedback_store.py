"""Feedback store — thumbs + comments on generated reports.

Stored per workspace; exportable as *candidate* eval cases for manual
review (``evaluation/feedback-queue/*.json``) — feedback is never added to
the gated EDD dataset automatically: a human must review and promote it.
"""

from __future__ import annotations

import builtins
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from app.services.sqlite_util import connect

DDL = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    section TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_ws ON feedback(workspace_id, created_at);
"""


class FeedbackStore:
    """Workspace-scoped thumbs/comments; export produces eval-case drafts."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._initialized = False

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            if not self._initialized:
                await db.executescript(DDL)
                await db.commit()
                self._initialized = True
            yield db

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
        async with self._conn() as db:
            cur = await db.execute(
                "INSERT INTO feedback"
                " (workspace_id, job_id, section, rating, comment, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (workspace_id, job_id, section, rating, comment, time.time()),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def list(
        self, workspace_id: str, *, limit: int = 100
    ) -> builtins.list[dict[str, Any]]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM feedback WHERE workspace_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (workspace_id, limit),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def export_cases(
        self, workspace_id: str, *, limit: int = 500
    ) -> builtins.list[dict[str, Any]]:
        """Render feedback rows as candidate eval-case drafts (JSON dicts).

        The export carries the brief + the operator's signal so a human can
        review and promote worthy rows into the gated dataset — never
        auto-added (see ``scripts/export_feedback.py``).
        """
        rows = await self.list(workspace_id, limit=limit)
        cases: builtins.list[dict[str, Any]] = []
        for r in rows:
            cases.append(
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
            )
        return cases

    async def close(self) -> None:
        # Each _conn() opens a fresh connection used as a context manager —
        # nothing persistent to release; kept for symmetry with other stores.
        return None


def dumps_case(case: dict[str, Any]) -> str:
    """Stable JSON for the feedback-queue export files."""
    return json.dumps(case, indent=2, sort_keys=True) + "\n"
