"""LangGraph checkpointer factory — SQLite persistence per ``job_id``.

The checkpointer is what makes ``interrupt()``/resume and crash recovery
real: graph state persists across invocations and process restarts.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def sqlite_checkpointer(db_path: str) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """Yield an ``AsyncSqliteSaver`` bound to ``db_path`` (created if needed)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver
