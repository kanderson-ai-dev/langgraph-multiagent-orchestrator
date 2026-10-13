"""SSE event bus seam — ``EventBus`` protocol + in-memory and Redis backends.

The runner publishes per-node job events; API subscribers stream them.
``InMemoryEventBus`` (default) buffers history per job and fans live
events out to per-subscriber queues. ``RedisEventBus`` replicates the
semantics across replicas: history lives in a bounded Redis list, live
fan-out in Redis pub/sub — a subscriber on any replica sees every event.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

History = list[dict[str, Any]]


@runtime_checkable
class EventBus(Protocol):
    """Per-job event channel: replayable history + live subscription."""

    async def publish(self, job_id: str, event: dict[str, Any]) -> None: ...

    async def history(self, job_id: str) -> History: ...

    def subscribe(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered events first, then live ones, until a terminal."""
        ...


_HISTORY_MAX = 512


class InMemoryEventBus:
    """Default backend — per-process buffers and queues."""

    def __init__(self) -> None:
        self._events: dict[str, History] = {}
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        history = self._events.setdefault(job_id, [])
        history.append(event)
        if len(history) > _HISTORY_MAX:
            del history[:-_HISTORY_MAX]
        for q in self._queues.get(job_id, []):
            q.put_nowait(event)

    async def history(self, job_id: str) -> History:
        return list(self._events.get(job_id, []))

    async def subscribe(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues.setdefault(job_id, []).append(q)
        try:
            terminal_seen = False
            for e in await self.history(job_id):
                yield e
                terminal_seen = terminal_seen or bool(e.get("terminal"))
            while not terminal_seen:
                event = await q.get()
                yield event
                terminal_seen = bool(event.get("terminal"))
        finally:
            self._queues[job_id].remove(q)


class RedisEventBus:
    """Redis backend — RPUSH/LTRIM history + PUB/SUB fan-out.

    History entries expire with the channel TTL so abandoned jobs never
    grow Redis unboundedly.
    """

    def __init__(
        self,
        redis: Any,
        *,
        ttl_seconds: int = 3600,
        history_max: int = _HISTORY_MAX,
        poll_timeout: float = 30.0,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._history_max = history_max
        self._poll_timeout = poll_timeout

    @staticmethod
    def _hist_key(job_id: str) -> str:
        return f"events:{job_id}:hist"

    @staticmethod
    def _chan(job_id: str) -> str:
        return f"events:{job_id}"

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        payload = json.dumps(event, default=str)
        hist = self._hist_key(job_id)
        pipe = self._redis.pipeline()
        pipe.rpush(hist, payload)
        pipe.ltrim(hist, -self._history_max, -1)
        pipe.expire(hist, self._ttl)
        pipe.publish(self._chan(job_id), payload)
        await pipe.execute()

    async def history(self, job_id: str) -> History:
        rows = await self._redis.lrange(self._hist_key(job_id), 0, -1)
        return [json.loads(r) for r in rows]

    async def subscribe(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        chan = self._chan(job_id)
        terminal_seen = False
        for e in await self.history(job_id):
            yield e
            terminal_seen = terminal_seen or bool(e.get("terminal"))
        if terminal_seen:
            return
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(chan)
            while not terminal_seen:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=self._poll_timeout,
                )
                if msg is None:
                    continue
                event = json.loads(msg["data"])
                yield event
                terminal_seen = bool(event.get("terminal"))
        finally:
            await pubsub.unsubscribe(chan)
            await pubsub.aclose()


def build_event_bus(settings: Any) -> EventBus:
    """Factory: Redis bus when ``REDIS_URL`` is set, in-memory else."""
    if getattr(settings, "redis_url", None) is not None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True
        )
        return RedisEventBus(client)
    return InMemoryEventBus()
