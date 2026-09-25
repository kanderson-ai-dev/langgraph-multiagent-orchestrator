"""Sliding-window rate limiter semantics."""

from app.core.rate_limit import RateLimiter


def test_allows_within_budget_and_rejects_over() -> None:
    rl = RateLimiter(limit=3, window_seconds=60)
    assert rl.allow("client") is True
    assert rl.allow("client") is True
    assert rl.allow("client") is True
    assert rl.allow("client") is False


def test_window_slides() -> None:
    rl = RateLimiter(limit=2, window_seconds=60)
    t0 = 1000.0
    assert rl.allow("c", now=t0)
    assert rl.allow("c", now=t0 + 1)
    assert not rl.allow("c", now=t0 + 2)
    # After the window, oldest events expire
    assert rl.allow("c", now=t0 + 61)


def test_keys_are_independent() -> None:
    rl = RateLimiter(limit=1)
    assert rl.allow("a")
    assert rl.allow("b")


def test_zero_limit_disables_enforcement() -> None:
    rl = RateLimiter(limit=0)
    for _ in range(100):
        assert rl.allow("c")
