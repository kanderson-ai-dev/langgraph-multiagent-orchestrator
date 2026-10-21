"""SSE stream hygiene: heartbeat comments, lifetime cap, terminal close.

A job parked at ``awaiting_review`` (budget_usd=0 escalates immediately)
keeps its stream open — the exact case proxies used to kill on idle. The
route emits ``: ping`` comment frames at SSE_HEARTBEAT_SECONDS and a
``{"type":"timeout"}`` frame at SSE_MAX_STREAM_SECONDS. Every ``data:``
frame carries an ``id:`` line (the event's per-job seq) so a re-attach
sending ``Last-Event-ID`` replays only what it missed.
"""

import json
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


def _read_frames(stream: Any, *, max_lines: int = 300) -> list[tuple[str | None, Any]]:
    """Collect ``(id, parsed-data)`` SSE frames until the stream ends or
    the line budget is spent (history flushes first; later lines are
    heartbeats)."""
    frames: list[tuple[str | None, Any]] = []
    cur_id: str | None = None
    data: list[str] = []
    for i, line in enumerate(stream.iter_lines()):
        if i > max_lines:
            break
        if line.startswith("id:"):
            cur_id = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].strip())
        elif line == "":
            if data:
                frames.append((cur_id, json.loads("\n".join(data))))
            cur_id, data = None, []
    return frames


def _stream_frames(client: Any, job_id: str, **headers: str) -> list[tuple[str | None, Any]]:
    """Read a stream to completion. TestClient buffers StreamingResponse
    bodies, so callers must use a small ``sse_max_stream_seconds`` — the
    stream ends with a trailing ``{"type": "timeout"}`` frame."""
    url = f"/api/v1/orchestration/reports/{job_id}/stream"
    with client.stream("GET", url, headers=headers) as r:
        assert r.status_code == 200
        return _read_frames(r)


def _event_frames(frames: list[tuple[str | None, Any]]) -> list[tuple[str | None, Any]]:
    """Drop the closing ``timeout`` frame — it is not a job event and
    carries no ``id:``."""
    return [f for f in frames if f[1].get("type") != "timeout"]


_REPLAY_SETTINGS: dict[str, Any] = {
    "sse_heartbeat_seconds": 0.05,
    "sse_max_stream_seconds": 0.4,
}


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


def test_stream_frames_carry_monotonic_ids(tmp_path: Any) -> None:
    """Every data frame gets an ``id:`` line — the seq the client echoes
    back as ``Last-Event-ID`` after a reconnect."""
    with _client(tmp_path, **_REPLAY_SETTINGS) as client:
        job_id = _parked_job(client)
        frames = _event_frames(_stream_frames(client, job_id))
    assert len(frames) >= 2
    ids = [i for i, _ in frames]
    assert all(i is not None for i in ids)
    int_ids = [int(i) for i in ids if i is not None]
    assert int_ids == sorted(int_ids)


def test_last_event_id_replays_only_newer_events(tmp_path: Any) -> None:
    """Reconnect with ``Last-Event-ID: <n>`` → exactly the events after n,
    no duplicates, no gaps."""
    import time

    with _client(tmp_path, **_REPLAY_SETTINGS) as client:
        job_id = _parked_job(client)
        time.sleep(0.2)  # let post-escalation events settle
        full = _event_frames(_stream_frames(client, job_id))
        assert len(full) >= 2
        cut = full[len(full) // 2][0]
        expected = [f for f in full if int(f[0] or "0") > int(cut or "0")]
        replayed = _event_frames(
            _stream_frames(client, job_id, **{"Last-Event-ID": str(cut)})
        )
    assert replayed == expected


def test_last_event_id_invalid_values_behave_as_absent(tmp_path: Any) -> None:
    """Non-integer and negative ids must not 500 — treated as no header."""
    with _client(tmp_path, **_REPLAY_SETTINGS) as client:
        job_id = _parked_job(client)
        full = _event_frames(_stream_frames(client, job_id))
        for bad in ("abc", "-3", "1.5"):
            replayed = _event_frames(
                _stream_frames(client, job_id, **{"Last-Event-ID": bad})
            )
            assert replayed == full


def test_last_event_id_beyond_end_replays_nothing(tmp_path: Any) -> None:
    """An id past the last event replays no history; the stream itself
    stays alive (heartbeats flow until the lifetime cap)."""
    with _client(tmp_path, **_REPLAY_SETTINGS) as client:
        job_id = _parked_job(client)
        frames = _event_frames(
            _stream_frames(client, job_id, **{"Last-Event-ID": "999999"})
        )
    assert frames == []


def test_last_event_id_cannot_cross_tenants(tmp_path: Any) -> None:
    """Replay honours the workspace scoping — a foreign job id is still a
    generic 404 even with a well-formed Last-Event-ID."""
    from app.core.security import hash_password, issue_token
    from app.services.workspace_store import WorkspaceStore

    settings = Settings(
        _env_file=None,
        scrape_delay_seconds=0.0,
        scrape_respect_robots=False,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
        **_REPLAY_SETTINGS,
    )
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=MemorySaver(),
    )
    db = str(tmp_path / "sse-tenant.sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    app.state.workspace_store = WorkspaceStore(db)

    def _h(ws: str) -> dict[str, str]:
        return {
            "Authorization": (
                f"Bearer {issue_token(settings, subject='u', workspace_id=ws, role='admin')}"
            )
        }

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/orchestration/reports",
            json={"topic": "acme internal report", "budget_usd": 0},
            headers=_h("acme"),
        )
        job_id = r.json()["job_id"]
        url = f"/api/v1/orchestration/reports/{job_id}/stream"
        foreign = {**_h("ws-b"), "Last-Event-ID": "1"}
        assert client.get(url, headers=foreign).status_code == 404
        with client.stream("GET", url, headers={**_h("acme"), "Last-Event-ID": "1"}) as r2:
            assert r2.status_code == 200


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
