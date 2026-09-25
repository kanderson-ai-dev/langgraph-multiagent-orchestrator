"""In-memory sliding-window rate limiter (per API key/IP).

Suitable for single-process deployments; a distributed store (Redis) is a
documented next step for multi-replica setups.
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window counter: at most ``limit`` events per ``window_seconds``."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an event for ``key``; returns True if within budget."""
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

    def remaining(self, key: str, *, now: float | None = None) -> int:
        """Remaining budget for ``key`` (for informational headers)."""
        if self.limit <= 0:
            return self.limit
        now = time.monotonic() if now is None else now
        events = self._events[key]
        while events and now - events[0] >= self.window_seconds:
            events.popleft()
        return max(0, self.limit - len(events))
