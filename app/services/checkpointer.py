"""LangGraph checkpointer factory — per-``job_id`` state persistence.

The checkpointer is what makes ``interrupt()``/resume and crash recovery
real: graph state persists across invocations and process restarts.

Backends:

- ``sqlite_checkpointer`` — default; one local file, single node.
- ``postgres_checkpointer`` — ``STORAGE_BACKEND=postgres``; shares the
  stores' DSN so HITL resume works across *any* replica of the app.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def sqlite_checkpointer(db_path: str) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """Yield an ``AsyncSqliteSaver`` bound to ``db_path`` (created if needed)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver


@asynccontextmanager
async def postgres_checkpointer(dsn: str) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Yield an ``AsyncPostgresSaver`` on an app-lifespan-owned pool.

    ``AsyncPostgresSaver`` requires autocommit connections and creates
    its checkpoint tables on ``setup()`` — both handled here so the
    caller just picks the backend by setting.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        dsn, open=False, kwargs={"autocommit": True}
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver
    finally:
        await pool.close()
