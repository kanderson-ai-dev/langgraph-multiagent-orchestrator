"""Process-local state hygiene: bounded A/B run cache persisted to the
registry DB, JobRunner dicts freed on terminal states, and prompt-registry
freshness across processes sharing one SQLite file."""

import time
from typing import Any

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import SecretStr

from app.api.v1.routes.prompts import _AB_RUN_CACHE_MAX, get_prompt_evaluator
from app.core.config import Settings, get_settings
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.prompt_registry import PromptRegistry
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm
from tests.test_prompt_lifecycle import _CLEAN, _ab


def _app(
    tmp_path: Any, registry: PromptRegistry, **overrides: Any
) -> Any:
    settings = Settings(
        _env_file=None,
        scrape_delay_seconds=0.0,
        scrape_respect_robots=False,
        openai_api_key=SecretStr("fake-test-key"),
        **overrides,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = str(tmp_path / "state.sqlite")
    app.state.runner = JobRunner(
        _graph(settings), JobStore(db), UsageStore(db), settings,
        prompt_registry=registry,
    )
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    app.state.prompt_registry = registry
    return app


def _graph(settings: Settings) -> Any:
    from app.graph.graph import build_graph

    return build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=MemorySaver(),
    )


def _stub_evaluator(client: TestClient) -> None:
    async def evaluator(name: str, version: int) -> Any:
        return _ab(name, max(version - 1, 1), version, _CLEAN, _CLEAN)

    client.app.dependency_overrides[get_prompt_evaluator] = lambda: evaluator  # type: ignore[attr-defined]


def _wait_run(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        r = client.get(f"/api/v1/prompts/ab-runs/{run_id}")
        body = r.json()
        if r.status_code == 200 and body["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"ab run {run_id} never finished")


# ─── ab_runs: bounded cache + persistence ───────────────────────────────────


def test_live_ab_run_persists_across_restart(tmp_path: Any) -> None:
    db = str(tmp_path / "registry.sqlite")
    registry = PromptRegistry.load(db_path=db)
    with TestClient(_app(tmp_path, registry)) as client:
        _stub_evaluator(client)
        c = client.post(
            "/api/v1/prompts/writer/candidates",
            json={"template": "Writer v2 candidate.", "changelog": "live run"},
        )
        assert c.status_code == 201, c.text
        r = client.post("/api/v1/prompts/writer/ab-test", json={"live": True})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        done = _wait_run(client, run_id)
        assert done["status"] == "done"
        assert done["name"] == "writer"

    # Simulate a restart: drop the whole process-local cache — the
    # persisted registry row must still answer, workspace-scoped.
    registry2 = PromptRegistry.load(db_path=db)
    row = registry2.get_ab_run(run_id, "default")
    assert row is not None
    assert row["status"] == "done"
    assert registry2.get_ab_run(run_id, "other-workspace") is None


def test_ab_cache_is_bounded(tmp_path: Any) -> None:
    from app.api.v1.routes.prompts import _ab_cache_put

    runs: dict[str, dict[str, Any]] = {}
    for i in range(_AB_RUN_CACHE_MAX + 25):
        _ab_cache_put(runs, f"r{i}", {"status": "done"})
    assert len(runs) == _AB_RUN_CACHE_MAX
    assert "r0" not in runs  # oldest evicted
    assert f"r{_AB_RUN_CACHE_MAX + 24}" in runs


def test_ab_cache_ttl_evicts(tmp_path: Any) -> None:
    import time as _t

    from app.api.v1.routes.prompts import _ab_cache_put

    runs = {"old": {"status": "done", "_ts": _t.monotonic() - 7200.0}}
    _ab_cache_put(runs, "new", {"status": "running"})
    assert "old" not in runs
    assert runs["new"]["status"] == "running"


# ─── JobRunner dicts are freed on terminal states ────────────────────────────


def test_runner_state_freed_after_completion(tmp_path: Any) -> None:
    registry = PromptRegistry.load(db_path=":memory:")
    app = _app(tmp_path, registry)
    runner = app.state.runner
    with TestClient(app) as client:
        for i in range(3):
            client.post(
                "/api/v1/orchestration/reports",
                json={"topic": f"state cleanup {i}", "budget_usd": 1.0},
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            jobs = client.get("/api/v1/orchestration/reports").json()
            done = jobs and all(
                j["status"] in ("done", "failed", "blocked") for j in jobs
            ) and len(jobs) == 3
            # A job's status lands terminal before its task's finally-block
            # reaps the runner dicts — poll for both, not just the first.
            if done and not runner._tasks:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("jobs never finished")
        assert runner._tasks == {}
        assert runner._last_values == {}
        assert runner._trace_ids == {}


# ─── registry freshness across processes ────────────────────────────────────


def test_registry_refresh_across_processes(tmp_path: Any) -> None:
    """Two registry instances on one DB file see each other's mutations."""
    db = str(tmp_path / "shared.sqlite")
    a = PromptRegistry.load(db_path=db)
    b = PromptRegistry.load(db_path=db)

    before = {pv.version for pv in b.list_versions("writer")}
    cand = a.create_candidate("writer", "You are Writer v2 (cross-process).",
                              "cross-process candidate", author="proc-a")
    after = {pv.version for pv in b.list_versions("writer")}
    assert cand.version in after - before

    a.promote("writer", cand.version, actor="proc-a", reason="x" * 25)
    promoted_b = b.get("writer")
    assert promoted_b.version == cand.version
    # A sees its own write without a refresh round-trip too.
    assert a.get("writer").version == cand.version
