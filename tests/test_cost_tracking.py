"""Cost tracking: per-role accounting, tracker binding, persistence."""

import pytest

from app.core.config import Settings
from app.core.cost_tracking import (
    UsageTracker,
    bind_tracker,
    current_tracker,
    track_llm_call,
    tracked_structured,
)
from app.services.llm_client import StubLLM
from app.services.usage_store import UsageStore


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        cost_input_price_per_1m=1.0,   # $1/1M — easy math
        cost_output_price_per_1m=2.0,
    )


async def test_tracker_records_and_persists(
    settings: Settings, tmp_path: pytest.TempPathFactory
) -> None:
    store = UsageStore(str(tmp_path / "u.sqlite"))
    tracker = UsageTracker(
        job_id="j1", workspace_id="ws-a", store=store, settings=settings
    )
    rec = await tracker.record_call(
        role="writer", model="m", prompt_tokens=1_000_000, completion_tokens=500_000
    )
    # $1 input + $1 output
    assert rec.cost_usd == pytest.approx(2.0)
    assert tracker.total_cost == pytest.approx(2.0)
    assert await store.job_cost("j1") == pytest.approx(2.0)


async def test_context_binding_scopes_calls(
    settings: Settings, tmp_path: pytest.TempPathFactory
) -> None:
    store = UsageStore(str(tmp_path / "u.sqlite"))
    tracker = UsageTracker(
        job_id="j1", workspace_id="ws-a", store=store, settings=settings
    )
    # Outside a bound context, tracking is a no-op.
    assert current_tracker() is None
    await track_llm_call(role="x", model="m", prompt_tokens=1, completion_tokens=1)
    assert len(tracker.records) == 0

    async with bind_tracker(tracker):
        await track_llm_call(
            role="researcher", model="m", prompt_tokens=100, completion_tokens=50
        )
    assert len(tracker.records) == 1
    assert tracker.records[0].role == "researcher"
    # Unbound again after exit.
    assert current_tracker() is None


async def test_tracked_structured_records_usage(
    settings: Settings, tmp_path: pytest.TempPathFactory
) -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        value: str = "ok"

    store = UsageStore(str(tmp_path / "u.sqlite"))
    tracker = UsageTracker(
        job_id="j2", workspace_id="ws-a", store=store, settings=settings
    )
    stub = StubLLM()
    stub.register("Out", lambda s, c: Out())

    async with bind_tracker(tracker):
        result = await tracked_structured(
            stub, Out, role="writer", model="stub",
            system="sys prompt", user="user prompt",
        )
    assert result.value == "ok"
    assert len(tracker.records) == 1
    assert tracker.records[0].prompt_tokens > 0
