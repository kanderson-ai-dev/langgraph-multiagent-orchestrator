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


# ─── seq stamping + Last-Event-ID replay ─────────────────────────────────────


async def test_publish_stamps_monotonic_seq(bus: EventBus) -> None:
    """Every published event carries a per-job monotonically increasing
    ``seq`` — the value the SSE ``id:`` line exposes for replay."""
    await bus.publish("j5", {"n": 1})
    await bus.publish("j5", {"n": 2})
    await bus.publish("j5", {"n": 3})
    assert [e["seq"] for e in await bus.history("j5")] == [1, 2, 3]


async def test_seq_is_per_job(bus: EventBus) -> None:
    await bus.publish("j6", {"n": 1})
    await bus.publish("j6", {"n": 2})
    await bus.publish("j7", {"n": 9})
    assert (await bus.history("j7"))[0]["seq"] == 1


async def test_subscribe_after_replays_only_newer(bus: EventBus) -> None:
    for i in range(1, 4):
        await bus.publish("j8", {"n": i, "terminal": i == 3})
    seen = [e async for e in bus.subscribe("j8", after=2)]
    assert [e["n"] for e in seen] == [3]


async def test_subscribe_at_end_still_streams_live(
    bus: EventBus,
) -> None:
    """A client caught up to the latest seq replays nothing but stays
    attached — the next live publish still reaches it."""
    await bus.publish("j9", {"n": 1})  # seq 1
    received: list[dict[str, Any]] = []

    async def listen() -> None:
        async for e in bus.subscribe("j9", after=1):
            received.append(e)

    task = asyncio.create_task(listen())
    # Wait until the subscriber is actually attached — the in-memory bus
    # exposes its queue registry; Redis pub/sub attach just needs a
    # couple of loop ticks before a publish can reach it.
    for _ in range(100):
        if isinstance(bus, InMemoryEventBus) and bus._queues.get("j9"):
            break
        if not isinstance(bus, InMemoryEventBus):
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    await bus.publish("j9", {"n": 2, "terminal": True})
    await asyncio.wait_for(task, timeout=5)
    assert [e["n"] for e in received] == [2]


async def test_subscribe_after_beyond_end_replays_nothing(
    bus: EventBus,
) -> None:
    """An id past every seq replays no history and (per Last-Event-ID
    semantics) filters older live events too — the client claims to have
    seen them already."""
    await bus.publish("j10b", {"n": 1, "terminal": True})
    seen = [e async for e in bus.subscribe("j10b", after=99)]
    assert seen == []


async def test_subscribe_caught_up_on_terminal_closes(bus: EventBus) -> None:
    """Re-attaching with an id at/past the terminal replays nothing and
    ends immediately instead of hanging on the live queue."""
    await bus.publish("j10", {"n": 1})
    await bus.publish("j10", {"n": 2, "terminal": True})
    seen = [e async for e in bus.subscribe("j10", after=2)]
    assert seen == []


async def test_subscribe_after_none_replays_everything(bus: EventBus) -> None:
    await bus.publish("j11", {"n": 1, "terminal": True})
    seen = [e async for e in bus.subscribe("j11")]
    assert [e["n"] for e in seen] == [1]


def test_factory_selects_backend(settings: Any) -> None:
    from pydantic import SecretStr

    assert isinstance(build_event_bus(settings), InMemoryEventBus)
    with_redis = settings.model_copy(
        update={"redis_url": SecretStr("redis://localhost:6379/0")}
    )
    assert isinstance(build_event_bus(with_redis), RedisEventBus)
