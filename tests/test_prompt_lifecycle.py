"""Prompt lifecycle API: candidates, A/B eval, gated promotion, rollback.

These tests exercise the *real* promotion gate — the offline A/B evaluator
runs the full graph against the EDD dataset — plus a dependency-injected
regressing evaluator to prove the gate blocks (and overrides) correctly.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.v1.routes.prompts import get_prompt_evaluator
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.graph.nodes.reviewer import ReviewOutput, Rubric
from app.main import create_app
from app.services.job_store import JobStore
from app.services.llm_client import StubLLM
from app.services.prompt_eval import (
    EDD_THRESHOLDS,
    ABResult,
    build_eval_stub,
    run_ab_eval,
)
from app.services.prompt_registry import PromptRegistry
from app.services.usage_store import UsageStore
from evaluation.dataset import DATASET

CANDIDATE_TEMPLATE = "You are the Writer v2. Produce an even better report."

_CLEAN = {
    "citation_support_rate": 1.0,
    "rubric_score_avg": 4.5,
    "debate_convergence_rate": 1.0,
    "blocked_rate": 1.0,
}
_REGRESSED = {**_CLEAN, "rubric_score_avg": 3.0, "debate_convergence_rate": 0.5}


def _ab(
    name: str,
    promoted_version: int,
    candidate_version: int,
    promoted: dict[str, float],
    candidate: dict[str, float],
) -> ABResult:
    return ABResult(
        name=name,
        promoted_version=promoted_version,
        candidate_version=candidate_version,
        promoted_metrics=promoted,
        candidate_metrics=candidate,
        deltas={k: round(candidate[k] - promoted[k], 6) for k in promoted},
    )


@pytest.fixture()
def prompt_registry() -> PromptRegistry:
    """YAML seeds + an in-memory lifecycle overlay — mutations don't touch disk."""
    return PromptRegistry.load(db_path=":memory:")


@pytest.fixture()
def client(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, prompt_registry: PromptRegistry
) -> Any:
    settings = Settings(_env_file=None)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = str(tmp_path / "test.sqlite")
    job_store = JobStore(db)
    usage_store = UsageStore(db)
    app.state.runner = SimpleNamespace(job_store=job_store, usage_store=usage_store)
    app.state.job_store = job_store
    app.state.usage_store = usage_store
    app.state.prompt_registry = prompt_registry
    with TestClient(app) as c:
        yield c


def _regressing_evaluator(
    client: TestClient, *, candidate: dict[str, float] | None = None
) -> None:
    """Inject an evaluator reporting a metric regression for the candidate."""
    candidate = candidate or _REGRESSED

    async def evaluator(name: str, version: int) -> ABResult:
        promoted_version = max(version - 1, 1)
        return _ab(name, promoted_version, version, _CLEAN, candidate)

    client.app.dependency_overrides[get_prompt_evaluator] = lambda: evaluator  # type: ignore[attr-defined]


def _create_candidate(client: TestClient, name: str = "writer") -> int:
    r = client.post(
        f"/api/v1/prompts/{name}/candidates",
        json={"template": CANDIDATE_TEMPLATE, "changelog": "test candidate"},
    )
    assert r.status_code == 201, r.text
    return int(r.json()["version"])


# ─── read endpoints ──────────────────────────────────────────────────────────


def test_list_prompts(client: TestClient) -> None:
    r = client.get("/api/v1/prompts")
    assert r.status_code == 200
    body = r.json()
    assert "writer" in body and "supervisor" in body
    writer_v1 = body["writer"][0]
    assert writer_v1["version"] == 1
    assert writer_v1["status"] == "promoted"
    assert writer_v1["sha256"]


def test_list_versions(client: TestClient) -> None:
    r = client.get("/api/v1/prompts/writer")
    assert r.status_code == 200
    assert [v["version"] for v in r.json()] == [1]


def test_unknown_prompt_404(client: TestClient) -> None:
    assert client.get("/api/v1/prompts/nosuch").status_code == 404
    assert client.get("/api/v1/prompts/nosuch/diff?from=1&to=2").status_code == 404
    assert (
        client.post(
            "/api/v1/prompts/nosuch/candidates", json={"template": "x"}
        ).status_code
        == 404
    )


# ─── candidates & diff ───────────────────────────────────────────────────────


def test_create_candidate_and_diff(client: TestClient) -> None:
    v = _create_candidate(client)
    assert v == 2
    r = client.get("/api/v1/prompts/writer")
    assert [pv["version"] for pv in r.json()] == [1, 2]
    assert r.json()[1]["status"] == "candidate"

    r = client.get("/api/v1/prompts/writer/diff?from=1&to=2")
    assert r.status_code == 200
    diff = r.json()["diff"]
    assert "--- writer v1" in diff and "+++ writer v2" in diff
    assert "+You are the Writer v2" in diff


def test_candidate_variable_mismatch(client: TestClient) -> None:
    """A candidate must honor the promoted template's variable contract."""
    r = client.post(
        "/api/v1/prompts/researcher/candidates",
        json={"template": "no placeholders here"},
    )
    assert r.status_code == 422
    ok = client.post(
        "/api/v1/prompts/researcher/candidates",
        json={"template": "use {max_queries} wisely"},
    )
    assert ok.status_code == 201


def test_diff_requires_params(client: TestClient) -> None:
    r = client.get("/api/v1/prompts/writer/diff")
    assert r.status_code == 422
    r = client.get("/api/v1/prompts/writer/diff?from=1&to=99")
    assert r.status_code == 404


# ─── A/B eval ────────────────────────────────────────────────────────────────


def test_ab_test_runs_real_eval(client: TestClient) -> None:
    """The default evaluator runs the real graph over the EDD dataset."""
    v = _create_candidate(client)
    r = client.post("/api/v1/prompts/writer/ab-test", json={"version": v})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["promoted_version"] == 1
    assert body["candidate_version"] == v
    # Stub pipeline: identical prompts → identical (clean) metrics, zero deltas.
    assert body["promoted_metrics"]["citation_support_rate"] == 1.0
    assert body["candidate_metrics"]["rubric_score_avg"] > 0
    # Gated metrics are deterministic under the stub — zero deltas.
    # (Informational metrics like latency legitimately differ per run;
    # metrics the stub eval never produces, e.g. claim support, are
    # simply absent from deltas.)
    for metric in EDD_THRESHOLDS:
        assert body["deltas"].get(metric, 0) == 0, metric


def test_ab_test_unknown_version(client: TestClient) -> None:
    r = client.post("/api/v1/prompts/writer/ab-test", json={"version": 99})
    assert r.status_code == 404


def test_ab_run_status_404(client: TestClient) -> None:
    r = client.get("/api/v1/prompts/ab-runs/nonexistent")
    assert r.status_code == 404


# ─── gated promotion ─────────────────────────────────────────────────────────


def test_promote_clean_candidate(client: TestClient) -> None:
    v = _create_candidate(client)
    r = client.post("/api/v1/prompts/writer/promote", json={"version": v})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "promoted"
    versions = {pv["version"]: pv["status"] for pv in client.get("/api/v1/prompts/writer").json()}
    assert versions == {1: "retired", 2: "promoted"}


def test_promote_blocked_on_regression(client: TestClient) -> None:
    v = _create_candidate(client)
    _regressing_evaluator(client)
    r = client.post("/api/v1/prompts/writer/promote", json={"version": v})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "promotion blocked by metric regression"
    assert any("rubric_score_avg" in f for f in detail["failures"])
    assert any("debate_convergence_rate" in f for f in detail["failures"])
    # Blocked promotion must not change lifecycle state.
    versions = {pv["version"]: pv["status"] for pv in client.get("/api/v1/prompts/writer").json()}
    assert versions == {1: "promoted", 2: "candidate"}


def test_promote_override_with_reason(client: TestClient) -> None:
    v = _create_candidate(client)
    _regressing_evaluator(client)
    r = client.post(
        "/api/v1/prompts/writer/promote",
        json={
            "version": v,
            "override_reason": "Known eval gap: candidate improves tone for exec audience",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "promoted"
    # The audit chain records the override action with metrics + reason.
    audit = client.get("/api/v1/prompts/audit").json()
    assert audit["valid"] is True
    override_events = [e for e in audit["entries"] if e["action"] == "override"]
    assert len(override_events) == 1
    assert override_events[0]["reason"].startswith("Known eval gap")


def test_promote_override_requires_reason(client: TestClient) -> None:
    """A regression plus a too-short reason still fails validation."""
    v = _create_candidate(client)
    _regressing_evaluator(client)
    r = client.post(
        "/api/v1/prompts/writer/promote",
        json={"version": v, "override_reason": "too short"},
    )
    assert r.status_code == 422


def test_promote_already_promoted(client: TestClient) -> None:
    r = client.post("/api/v1/prompts/writer/promote", json={"version": 1})
    assert r.status_code == 409


def test_promote_blocked_by_real_pipeline_regression(
    client: TestClient, prompt_registry: PromptRegistry
) -> None:
    """A version-keyed stub makes the candidate regress *through the real
    graph* — the gate sees real numbers, not an injected verdict."""
    r = client.post(
        "/api/v1/prompts/reviewer/candidates",
        json={"template": "You are the Reviewer v2. Grade more harshly."},
    )
    assert r.status_code == 201, r.text
    v = r.json()["version"]

    def _version_aware_stub() -> StubLLM:
        stub = build_eval_stub()

        def review(schema: type[BaseModel], context: dict[str, Any] | None) -> BaseModel:
            score = (
                3.0
                if context
                and context.get("prompt_name") == "reviewer"
                and context.get("prompt_version") != 1
                else 4.5
            )
            return ReviewOutput(
                verdict="approve",
                score=score,
                rubric=Rubric(
                    structure=score, clarity=score, grounding=score, tone=score
                ),
                feedback=[],
            )

        stub.register("ReviewOutput", review)
        return stub

    settings = Settings(
        _env_file=None, scrape_delay_seconds=0.0, scrape_respect_robots=False
    )

    async def evaluator(name: str, version: int) -> ABResult:
        return await run_ab_eval(
            settings,
            prompt_registry,
            name,
            version,
            cases=list(DATASET),
            llm=_version_aware_stub(),
        )

    client.app.dependency_overrides[get_prompt_evaluator] = lambda: evaluator  # type: ignore[attr-defined]

    r = client.post("/api/v1/prompts/reviewer/promote", json={"version": v})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert any("rubric_score_avg" in f for f in detail["failures"])
    metrics = detail["metrics"]
    # Structure is graded mechanically (5.0 — the stub markdown contains
    # every template heading); the promoted mean is (5.0+4.5×3)/4 = 4.625.
    assert metrics["promoted_metrics"]["rubric_score_avg"] == pytest.approx(4.625)
    # Candidate rubric dims are 3.0 but structure is mechanical (5.0):
    # the mean is (5.0+3.0×3)/4 = 3.5.
    assert metrics["candidate_metrics"]["rubric_score_avg"] == pytest.approx(3.5)
    # Under the rubric-derived verdict, a 3.0-scoring reviewer cannot
    # approve — convergence legitimately drops. That IS the regression the
    # promotion gate must see (advisory "approve" no longer masks it).
    assert metrics["candidate_metrics"]["debate_convergence_rate"] == 0.0


# ─── rollback & audit chain ──────────────────────────────────────────────────


def test_rollback_restores_previous(client: TestClient) -> None:
    v = _create_candidate(client)
    assert client.post("/api/v1/prompts/writer/promote", json={"version": v}).status_code == 200

    r = client.post(
        "/api/v1/prompts/writer/rollback",
        json={"to_version": 1, "reason": "candidate underperforms in prod"},
    )
    assert r.status_code == 200, r.text
    versions = {pv["version"]: pv["status"] for pv in client.get("/api/v1/prompts/writer").json()}
    assert versions == {1: "promoted", 2: "rolled_back"}

    audit = client.get("/api/v1/prompts/audit").json()
    assert audit["valid"] is True
    actions = [e["action"] for e in audit["entries"]]
    assert actions == ["candidate", "promote", "rollback"]
    assert audit["entries"][-1]["reason"] == "candidate underperforms in prod"


def test_rollback_requires_reason(client: TestClient) -> None:
    r = client.post(
        "/api/v1/prompts/writer/rollback", json={"to_version": 1}
    )
    assert r.status_code == 422


def test_rollback_same_version_conflict(client: TestClient) -> None:
    r = client.post(
        "/api/v1/prompts/writer/rollback",
        json={"to_version": 1, "reason": "already promoted"},
    )
    assert r.status_code == 409


def test_audit_chain_integrity(client: TestClient) -> None:
    """Every lifecycle event is hash-chained; verification passes on read."""
    _create_candidate(client)
    audit = client.get("/api/v1/prompts/audit").json()
    assert audit["valid"] is True
    assert len(audit["entries"]) == 1
    entry = audit["entries"][0]
    assert entry["action"] == "candidate"
    assert entry["hash"] == entry["recomputed_hash"]
    assert entry["prev_hash"] != entry["hash"]


# ─── auth ────────────────────────────────────────────────────────────────────


def test_auth_required_when_enabled(tmp_path: Any) -> None:
    """With JWT auth configured, lifecycle routes require a Bearer token."""
    settings = Settings(
        _env_file=None,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = str(tmp_path / "auth.sqlite")
    app.state.runner = SimpleNamespace(
        job_store=JobStore(db), usage_store=UsageStore(db)
    )
    app.state.prompt_registry = PromptRegistry.load(db_path=":memory:")
    with TestClient(app) as c:
        assert c.get("/api/v1/prompts").status_code == 401
        tok = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "pw", "workspace_id": "ws-a"},
        ).json()["access_token"]
        r = c.get("/api/v1/prompts", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
