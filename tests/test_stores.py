"""JobStore multi-tenant isolation and UsageStore aggregation."""

import pytest

from app.graph.state import Brief
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore


@pytest.fixture()
def brief(brief_kwargs: dict[str, object]) -> Brief:
    return Brief.model_validate(brief_kwargs)


async def test_job_crud_and_workspace_isolation(
    tmp_path: pytest.TempPathFactory, brief: Brief
) -> None:
    store = JobStore(str(tmp_path / "jobs.sqlite"))
    job = await store.create("ws-a", brief)
    assert job.status == "queued"
    assert job.job_id

    fetched = await store.get("ws-a", job.job_id)
    assert fetched is not None and fetched.brief.topic == brief.topic

    # A different workspace must not see the job — even knowing the id.
    assert await store.get("ws-b", job.job_id) is None
    assert await store.update_status("ws-b", job.job_id, "failed") is None

    updated = await store.update_status("ws-a", job.job_id, "running")
    assert updated is not None and updated.status == "running"

    done = await store.set_result("ws-a", job.job_id, {"report": "# ok"}, cost_usd=0.012)
    assert done is not None and done.status == "done" and done.cost_usd == 0.012

    listed_a = await store.list("ws-a")
    listed_b = await store.list("ws-b")
    assert [j.job_id for j in listed_a] == [job.job_id]
    assert listed_b == []


async def test_usage_store_aggregation(tmp_path: pytest.TempPathFactory) -> None:
    store = UsageStore(str(tmp_path / "usage.sqlite"))
    await store.record(
        job_id="j1", workspace_id="ws-a", role="writer", model="m",
        prompt_tokens=100, completion_tokens=50, cost_usd=0.001, latency_ms=200,
    )
    await store.record(
        job_id="j1", workspace_id="ws-a", role="reviewer", model="m",
        prompt_tokens=80, completion_tokens=20, cost_usd=0.0005, latency_ms=100,
    )
    await store.record(
        job_id="j2", workspace_id="ws-b", role="writer", model="m", cost_usd=9.0,
    )

    assert await store.job_cost("j1") == pytest.approx(0.0015)

    summary = await store.workspace_summary("ws-a")
    assert summary.event_count == 2
    assert summary.total_cost_usd == pytest.approx(0.0015)
    assert summary.by_role["writer"] == pytest.approx(0.001)
    assert summary.by_role["reviewer"] == pytest.approx(0.0005)

    # Tenant isolation on aggregates.
    other = await store.workspace_summary("ws-b")
    assert other.event_count == 1
    assert "reviewer" not in other.by_role

    empty = await store.workspace_summary("ws-c")
    assert empty.event_count == 0 and empty.total_cost_usd == 0.0
