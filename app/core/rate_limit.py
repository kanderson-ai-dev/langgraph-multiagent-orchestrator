"""Rate limiter seam — sliding-window ``RateLimiter`` protocol + backends.

``InMemoryRateLimiter`` is the default (single process, zero deps).
``RedisRateLimiter`` gives the same sliding-window semantics across
replicas; it is selected automatically when ``REDIS_URL`` is configured
(see ``build_rate_limiter``). Both share one contract so callers never
know which backend they hold.
"""

import time
import uuid
from collections import defaultdict, deque
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    """Sliding-window contract: at most ``limit`` events per window."""

    async def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an event for ``key``; True when within budget."""
        ...

    async def remaining(self, key: str, *, now: float | None = None) -> int:
        """Remaining budget for ``key`` (informational headers)."""
        ...


class InMemoryRateLimiter:
    """Default backend — in-process sliding window, no dependencies."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    async def allow(self, key: str, *, now: float | None = None) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        events = self._events[key]
        while events and now - events[0] >= self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True

    async def remaining(self, key: str, *, now: float | None = None) -> int:
        if self.limit <= 0:
            return self.limit
        now = time.monotonic() if now is None else now
        events = self._events[key]
        while events and now - events[0] >= self.window_seconds:
            events.popleft()
        return max(0, self.limit - len(events))


# Atomic sliding window: drop expired members, count, admit + record.
_LUA_ALLOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  return {0, limit - count}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window))
return {1, limit - count - 1}
"""


class RedisRateLimiter:
    """Redis backend — same semantics, shared across replicas.

    The admit decision is a single Lua script (atomic); scores are wall
    clock because members live server-side. Fail-closed by design is NOT
    chosen here: a Redis outage raises — callers see 500, not a silent
    allow/deny they can't reason about.
    """

    def __init__(
        self, redis: Any, limit: int, window_seconds: float = 60.0
    ) -> None:
        self._redis = redis
        self.limit = limit
        self.window_seconds = window_seconds
        self._script = redis.register_script(_LUA_ALLOW)

    @staticmethod
    def _key(key: str) -> str:
        return f"rate_limit:{key}"

    async def allow(self, key: str, *, now: float | None = None) -> bool:
        if self.limit <= 0:
            return True
        t = time.time() if now is None else now
        member = f"{t}:{uuid.uuid4().hex[:8]}"
        result = await self._script(
            keys=[self._key(key)],
            args=[str(t), str(self.window_seconds), str(self.limit), member],
        )
        return bool(result[0])

    async def remaining(self, key: str, *, now: float | None = None) -> int:
        if self.limit <= 0:
            return self.limit
        t = time.time() if now is None else now
        k = self._key(key)
        await self._redis.zremrangebyscore(k, "-inf", t - self.window_seconds)
        count = int(await self._redis.zcard(k))
        return max(0, self.limit - count)


def build_rate_limiter(settings: Any) -> RateLimiter:
    """Factory: Redis backend when ``REDIS_URL`` is set, in-memory else."""
    if getattr(settings, "redis_url", None) is not None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True
        )
        return RedisRateLimiter(
            client,
            limit=settings.rate_limit_per_minute,
            window_seconds=60.0,
        )
    return InMemoryRateLimiter(limit=settings.rate_limit_per_minute)
