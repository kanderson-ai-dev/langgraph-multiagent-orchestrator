"""Job event bus — contract suite over every backend.

``InMemoryEventBus`` (default) and ``RedisEventBus`` (opt-in via
REDIS_URL) share identical semantics: buffered history replays first,
live events stream to subscribers, and a ``terminal`` event ends the
subscription. CI uses fakeredis — no real Redis needed.
"""

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest

from app.core.event_bus import (
    EventBus,
    InMemoryEventBus,
    RedisEventBus,
    build_event_bus,
)


@pytest.fixture(params=["memory", "redis"])
async def bus(request: Any) -> EventBus:
    if request.param == "redis":
        return RedisEventBus(
            fakeredis.aioredis.FakeRedis(decode_responses=True),
            poll_timeout=0.05,
        )
    return InMemoryEventBus()


async def test_history_replays_to_late_subscribers(bus: EventBus) -> None:
    await bus.publish("j1", {"node": "a", "n": 1})
    await bus.publish("j1", {"node": "b", "n": 2, "terminal": True})

    seen = [e async for e in bus.subscribe("j1")]
    assert [e["n"] for e in seen] == [1, 2]


async def test_live_events_reach_subscribers(bus: EventBus) -> None:
    received: list[dict[str, Any]] = []

    async def listen() -> None:
        async for e in bus.subscribe("j2"):
            received.append(e)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)  # let the subscriber attach
    await asyncio.sleep(0)
    await bus.publish("j2", {"node": "a"})
    await bus.publish("j2", {"node": "b", "terminal": True})
    await asyncio.wait_for(task, timeout=5)
    assert [e.get("node") for e in received] == ["a", "b"]


async def test_terminal_event_ends_subscription(bus: EventBus) -> None:
    await bus.publish("j3", {"node": "done", "terminal": True})
    gen = bus.subscribe("j3")
    events = [e async for e in gen]
    assert len(events) == 1


async def test_history_is_bounded_and_ordered(bus: EventBus) -> None:
    for i in range(5):
        await bus.publish("j4", {"n": i})
    hist = await bus.history("j4")
    assert [e["n"] for e in hist] == [0, 1, 2, 3, 4]


async def test_jobs_are_isolated(bus: EventBus) -> None:
    await bus.publish("jA", {"n": 1})
    await bus.publish("jB", {"n": 2})
    assert len(await bus.history("jA")) == 1
    assert len(await bus.history("jB")) == 1


def test_factory_selects_backend(settings: Any) -> None:
    from pydantic import SecretStr

    assert isinstance(build_event_bus(settings), InMemoryEventBus)
    with_redis = settings.model_copy(
        update={"redis_url": SecretStr("redis://localhost:6379/0")}
    )
    assert isinstance(build_event_bus(with_redis), RedisEventBus)
