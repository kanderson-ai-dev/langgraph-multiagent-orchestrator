"""SSE stream hygiene: heartbeat comments, lifetime cap, terminal close.

A job parked at ``awaiting_review`` (budget_usd=0 escalates immediately)
keeps its stream open — the exact case proxies used to kill on idle. The
route emits ``: ping`` comment frames at SSE_HEARTBEAT_SECONDS and a
``{"type":"timeout"}`` frame at SSE_MAX_STREAM_SECONDS.
"""

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.event_bus import InMemoryEventBus
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _client(tmp_path: Any, **overrides: Any) -> Any:
    settings = Settings(
        _env_file=None,
        scrape_delay_seconds=0.0,
        scrape_respect_robots=False,
        **overrides,
    )
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=MemorySaver(),
    )
    db = str(tmp_path / "sse.sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    return TestClient(app)


def _parked_job(client: Any) -> str:
    """A job that escalates on budget=0 → status awaiting_review, stream open."""
    import time

    r = client.post(
        "/api/v1/orchestration/reports",
        json={"topic": "SSE probe", "budget_usd": 0},
    )
    job_id = r.json()["job_id"]
    for _ in range(200):
        body = client.get(f"/api/v1/orchestration/reports/{job_id}").json()
        if body["status"] == "awaiting_review":
            return job_id
        time.sleep(0.05)
    raise AssertionError("job never parked at awaiting_review")


def test_heartbeat_frames_on_idle_stream(tmp_path: Any) -> None:
    with _client(tmp_path, sse_heartbeat_seconds=0.05,
                 sse_max_stream_seconds=30.0) as client:
        job_id = _parked_job(client)
        pings = 0
        with client.stream(
            "GET", f"/api/v1/orchestration/reports/{job_id}/stream"
        ) as r:
            for i, line in enumerate(r.iter_lines()):
                if line == ": ping":
                    pings += 1
                    if pings >= 2:
                        break
                if i > 100:
                    break
        assert pings >= 2


def test_stream_has_lifetime_cap(tmp_path: Any) -> None:
    with _client(tmp_path, sse_heartbeat_seconds=10.0,
                 sse_max_stream_seconds=0.2) as client:
        job_id = _parked_job(client)
        saw_timeout = False
        with client.stream(
            "GET", f"/api/v1/orchestration/reports/{job_id}/stream"
        ) as r:
            for i, line in enumerate(r.iter_lines()):
                if '"type": "timeout"' in line or '"type":"timeout"' in line:
                    saw_timeout = True
                    break
                if i > 200:
                    break
        assert saw_timeout


def test_terminal_event_closes_stream(tmp_path: Any) -> None:
    """A finished job's stream ends by itself (no manual close needed)."""
    import time

    with _client(tmp_path) as client:
        r = client.post(
            "/api/v1/orchestration/reports", json={"topic": "SSE terminal"}
        )
        job_id = r.json()["job_id"]
        for _ in range(300):
            body = client.get(f"/api/v1/orchestration/reports/{job_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        lines: list[str] = []
        with client.stream(
            "GET", f"/api/v1/orchestration/reports/{job_id}/stream"
        ) as stream:
            for i, line in enumerate(stream.iter_lines()):
                lines.append(line)
                if i > 500:
                    break
    assert any('"terminal": true' in line or '"terminal":true' in line
               for line in lines)


async def test_closed_subscription_frees_the_bus() -> None:
    """Generator close/cancel must drop the subscriber queue."""
    import asyncio
    import contextlib

    bus = InMemoryEventBus()

    async def _drain() -> None:
        async for _ in bus.subscribe("jz"):
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.02)
    assert len(bus._queues["jz"]) == 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert bus._queues["jz"] == []
