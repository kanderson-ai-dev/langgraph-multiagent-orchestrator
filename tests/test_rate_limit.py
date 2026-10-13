"""Sliding-window rate limiter — contract suite over every backend.

Both ``InMemoryRateLimiter`` (default) and ``RedisRateLimiter`` (opt-in
via REDIS_URL) must satisfy identical semantics; CI uses fakeredis, so
no real Redis is ever required.
"""

from typing import Any

import fakeredis.aioredis
import pytest

from app.core.rate_limit import (
    InMemoryRateLimiter,
    RateLimiter,
    RedisRateLimiter,
)


@pytest.fixture(params=["memory", "redis"])
def limiter(request: Any) -> RateLimiter:
    if request.param == "redis":
        return RedisRateLimiter(
            fakeredis.aioredis.FakeRedis(decode_responses=True),
            limit=3,
            window_seconds=60,
        )
    return InMemoryRateLimiter(limit=3, window_seconds=60)


async def test_allows_within_budget_and_rejects_over(
    limiter: RateLimiter,
) -> None:
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is False


async def test_window_slides(limiter: RateLimiter) -> None:
    t0 = 1000.0
    assert await limiter.allow("client", now=t0)
    assert await limiter.allow("client", now=t0 + 1)
    assert await limiter.allow("client", now=t0 + 2)
    assert not await limiter.allow("client", now=t0 + 3)
    # After the window, oldest events expire.
    assert await limiter.allow("client", now=t0 + 61)


async def test_keys_are_independent(limiter: RateLimiter) -> None:
    assert await limiter.allow("a")
    assert await limiter.allow("b")
    assert await limiter.allow("c")


async def test_remaining_budget(limiter: RateLimiter) -> None:
    t0 = 2000.0
    assert await limiter.remaining("c", now=t0) == 3
    assert await limiter.allow("c", now=t0)
    assert await limiter.remaining("c", now=t0) == 2
    # Failed admits don't consume budget.
    await limiter.allow("c", now=t0)
    await limiter.allow("c", now=t0)
    assert not await limiter.allow("c", now=t0)
    assert await limiter.remaining("c", now=t0) == 0


async def test_zero_limit_disables_enforcement() -> None:
    for rl in (
        InMemoryRateLimiter(limit=0),
        RedisRateLimiter(
            fakeredis.aioredis.FakeRedis(decode_responses=True), limit=0
        ),
    ):
        for _ in range(10):
            assert await rl.allow("c")


def test_factory_selects_backend(settings: Any) -> None:
    from pydantic import SecretStr

    from app.core.rate_limit import build_rate_limiter

    assert isinstance(
        build_rate_limiter(settings), InMemoryRateLimiter
    )
    with_redis = settings.model_copy(
        update={"redis_url": SecretStr("redis://localhost:6379/0")}
    )
    assert isinstance(build_rate_limiter(with_redis), RedisRateLimiter)
