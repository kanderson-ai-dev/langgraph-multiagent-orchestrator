"""Composition-root factory for the app's persistence stores.

``storage_backend`` picks the implementation set; consumers only see the
protocols in ``store_protocols`` so swapping backends changes nothing in
request-path code.

- ``sqlite`` (default): the existing single-node stores on
  ``database_path``. Zero config, WAL-tuned, single-writer (ADR 0003).
- ``postgres``: asyncpg-backed stores on ``database_url`` — the
  multi-replica option. Requires the ``asyncpg`` extra and a reachable
  server; schemas auto-migrate on first use, same convention as SQLite.

An unknown backend is a startup error, not a silent fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.config import Settings
from app.services.cache_store import CacheStore
from app.services.feedback_store import FeedbackStore
from app.services.job_store import JobStore
from app.services.prompt_store import PromptStoreProtocol, SqlitePromptStore
from app.services.token_store import SqliteTokenStore, TokenStore
from app.services.usage_store import UsageStore
from app.services.workspace_store import WorkspaceStore

if TYPE_CHECKING:
    from app.services.store_protocols import (
        CacheStoreProtocol,
        FeedbackStoreProtocol,
        JobStoreProtocol,
        UsageStoreProtocol,
        WorkspaceStoreProtocol,
    )


@dataclass(frozen=True)
class AppStores:
    """One instance of every store, sharing the backend's connection."""

    job_store: JobStoreProtocol
    usage_store: UsageStoreProtocol
    workspace_store: WorkspaceStoreProtocol
    feedback_store: FeedbackStoreProtocol
    token_store: TokenStore
    cache_store: CacheStoreProtocol | None
    prompt_store: PromptStoreProtocol


def build_stores(settings: Settings) -> AppStores:
    """Construct the store set for ``settings.storage_backend``."""
    backend = settings.storage_backend
    if backend == "sqlite":
        path = settings.database_path
        return AppStores(
            job_store=JobStore(path),
            usage_store=UsageStore(path),
            workspace_store=WorkspaceStore(path),
            feedback_store=FeedbackStore(path),
            token_store=SqliteTokenStore(path),
            cache_store=CacheStore(path) if settings.cache_enabled else None,
            prompt_store=SqlitePromptStore(path),
        )
    if backend == "postgres":
        if settings.database_url is None:
            msg = "STORAGE_BACKEND=postgres requires DATABASE_URL to be set"
            raise RuntimeError(msg)
        try:
            from app.services.pg.stores import PostgresStores
        except ImportError as exc:  # asyncpg not installed
            msg = (
                "STORAGE_BACKEND=postgres requires the 'asyncpg' package "
                f"({exc})"
            )
            raise RuntimeError(msg) from exc
        dsn = settings.database_url.get_secret_value()
        pg = PostgresStores(dsn)
        from app.services.pg.prompt_store import PgPromptStore

        return AppStores(
            job_store=pg.job_store,
            usage_store=pg.usage_store,
            workspace_store=pg.workspace_store,
            feedback_store=pg.feedback_store,
            token_store=pg.token_store,
            cache_store=pg.cache_store if settings.cache_enabled else None,
            prompt_store=PgPromptStore(dsn),
        )
    msg = f"unknown storage_backend: {backend!r} (expected 'sqlite' or 'postgres')"
    raise RuntimeError(msg)
