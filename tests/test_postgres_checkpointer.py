"""Postgres LangGraph checkpointer — cross-instance HITL resume.

Runs only when ``TEST_DATABASE_URL`` points at a live server (same gate
as the store contract suite; CI's ``postgres-contract`` job). With
``STORAGE_BACKEND=postgres`` the checkpointer shares the stores' DSN —
this proves an ``interrupt()`` pause persists in Postgres and a *second,
independent* saver instance (a different pool — the equivalent of a
second app replica) can resume the same thread.

The test drives its own loop instead of pytest-asyncio's: psycopg's
async mode cannot run on Windows' ``ProactorEventLoop``, so on win32 we
force a selector loop. (A Windows *deployment* on the postgres backend
needs the equivalent — see the compose note.)
"""

import asyncio
import os
import sys
import uuid
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="needs TEST_DATABASE_URL pointing at a live Postgres",
)

DSN = os.environ.get("TEST_DATABASE_URL", "")


class _S(TypedDict):
    value: str


def _build_graph() -> Any:
    def node_a(state: _S) -> dict[str, str]:
        return {"value": state["value"] + "-a"}

    def node_b(state: _S) -> dict[str, str]:
        decision = interrupt({"question": "approve?"})
        return {"value": f"{state['value']}-{decision}"}

    builder = StateGraph(_S)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder


async def _roundtrip() -> None:
    from app.services.checkpointer import postgres_checkpointer

    thread = f"t-{uuid.uuid4().hex[:8]}"
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread}}
    builder = _build_graph()

    # Instance 1: run to the interrupt, checkpoint lands in Postgres.
    async with postgres_checkpointer(DSN) as saver1:
        g1 = builder.compile(checkpointer=saver1)
        await g1.ainvoke({"value": "x"}, cfg)
        snap1 = await g1.aget_state(cfg)
        assert snap1.next == ("b",)
    # saver1's pool is now closed — nothing in-process survives.

    # Instance 2 (fresh pool, fresh compile — the "other replica"): the
    # same thread is still paused, and the resume completes the run.
    async with postgres_checkpointer(DSN) as saver2:
        g2 = builder.compile(checkpointer=saver2)
        snap2 = await g2.aget_state(cfg)
        assert snap2.next == ("b",)
        out = await g2.ainvoke(Command(resume="approved"), cfg)
        assert out["value"] == "x-a-approved"


def test_postgres_checkpoint_roundtrip_and_cross_instance_resume() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
        )
    asyncio.run(_roundtrip())
