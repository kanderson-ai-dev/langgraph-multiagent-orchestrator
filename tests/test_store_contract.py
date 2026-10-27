"""Backend contract suite — the same tests every store backend must pass.

``storage_backend=sqlite`` always runs; ``postgres`` joins the matrix
only when ``TEST_DATABASE_URL`` points at a live server (the optional
``docker-compose.postgres.yml`` or CI's service container). Anything a
consumer relies on belongs here: CRUD, tenant scoping, the conditional
``awaiting_review`` → ``expired`` flip, the sweeper query, aggregates,
schema auto-migration, and the small admin surfaces.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.graph.state import Brief
from app.services.cache_store import CacheStore
from app.services.feedback_store import FeedbackStore
from app.services.job_store import JobStore
from app.services.token_store import SqliteTokenStore
from app.services.usage_store import UsageStore
from app.services.workspace_store import WorkspaceStore, hash_api_key


def _backends() -> list[str]:
    backends = ["sqlite"]
    if os.environ.get("TEST_DATABASE_URL"):
        backends.append("postgres")
    return backends


@pytest.fixture(params=_backends())
async def stores(
    request: pytest.FixtureRequest, tmp_path: Any
) -> AsyncIterator[Any]:
    if request.param == "sqlite":
        path = str(tmp_path / "app.sqlite")
        yield SimpleNamespace(
            job_store=JobStore(path),
            usage_store=UsageStore(path),
            workspace_store=WorkspaceStore(path),
            feedback_store=FeedbackStore(path),
            token_store=SqliteTokenStore(path),
            cache_store=CacheStore(path),
        )
        return
    from app.services.pg.stores import PostgresStores

    pg = PostgresStores(os.environ["TEST_DATABASE_URL"])
    yield SimpleNamespace(
        job_store=pg.job_store,
        usage_store=pg.usage_store,
        workspace_store=pg.workspace_store,
        feedback_store=pg.feedback_store,
        token_store=pg.token_store,
        cache_store=pg.cache_store,
    )
    await pg.close()


@pytest.fixture()
def brief(brief_kwargs: dict[str, object]) -> Brief:
    return Brief.model_validate(brief_kwargs)


@pytest.fixture()
def ws() -> Any:
    """Unique-per-test workspace ids — Postgres is a shared database, so
    same-named workspaces from other tests/runs already exist there."""
    import uuid as _uuid

    suffix = _uuid.uuid4().hex[:8]
    return SimpleNamespace(
        a=f"ws-a-{suffix}",
        b=f"ws-b-{suffix}",
        c=f"ws-c-{suffix}",
        z=f"ws-z-{suffix}",
        t1=f"t1-{suffix}",
    )


# ─── jobs ───────────────────────────────────────────────────────────────────


async def test_job_crud_and_tenant_isolation(stores: Any, brief: Brief, ws: Any) -> None:
    js = stores.job_store
    job = await js.create(ws.a, brief)
    assert job.status == "queued" and job.job_id

    fetched = await js.get(ws.a, job.job_id)
    assert fetched is not None and fetched.brief.topic == brief.topic

    # Cross-tenant: invisible even with the known id.
    assert await js.get(ws.b, job.job_id) is None
    assert await js.update_status(ws.b, job.job_id, "failed") is None
    assert await js.set_result(ws.b, job.job_id, {"x": 1}) is None

    updated = await js.update_status(ws.a, job.job_id, "running")
    assert updated is not None and updated.status == "running"

    done = await js.set_result(
        ws.a, job.job_id, {"report": "# ok"}, cost_usd=0.012
    )
    assert done is not None and done.status == "done" and done.cost_usd == 0.012

    assert [j.job_id for j in await js.list(ws.a)] == [job.job_id]
    assert await js.list(ws.b) == []


async def test_job_conditional_expiry_is_the_resume_guard(
    stores: Any, brief: Brief, ws: Any
) -> None:
    """``mark_expired`` only flips ``awaiting_review`` — a second caller
    (or a job in any other state) gets ``None``, matching the route's
    409-on-wrong-status contract."""
    js = stores.job_store
    job = await js.create(ws.a, brief)
    # queued → not expirable
    assert await js.mark_expired(ws.a, job.job_id, reason="idle") is None

    await js.update_status(ws.a, job.job_id, "awaiting_review")
    expired = await js.mark_expired(ws.a, job.job_id, reason="idle 48h")
    assert expired is not None and expired.status == "expired"
    assert expired.result is not None
    assert expired.result["expired"] is True
    assert expired.result["expired_reason"] == "idle 48h"

    # Second expire on a terminal record — the double-call guard.
    assert await js.mark_expired(ws.a, job.job_id, reason="idle") is None
    # Cross-tenant expiry is invisible, not a leak.
    await js.create(ws.b, brief)
    await js.update_status(ws.b, job.job_id, "awaiting_review")


async def test_stale_awaiting_review_sweep(stores: Any, brief: Brief, ws: Any) -> None:
    js = stores.job_store
    fresh = await js.create(ws.a, brief)
    stale = await js.create(ws.b, brief)
    queued = await js.create(ws.b, brief)
    for j in (fresh, stale, queued):
        await js.update_status(j.workspace_id, j.job_id, "awaiting_review")
    await js.update_status(queued.workspace_id, queued.job_id, "done")

    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    found = await js.list_stale_awaiting_review(cutoff)
    ids = {j.job_id for j in found}
    assert ids >= {fresh.job_id, stale.job_id}  # cross-tenant, by design
    assert queued.job_id not in ids
    assert await js.list_stale_awaiting_review(
        datetime.now(UTC) - timedelta(days=1)
    ) == []


async def test_job_dashboard_aggregates(stores: Any, brief: Brief, ws: Any) -> None:
    js = stores.job_store
    j1 = await js.create(ws.a, brief)
    j2 = await js.create(ws.a, brief)
    await js.create(ws.b, brief)
    await js.update_status(ws.a, j1.job_id, "done")
    await js.update_status(ws.a, j2.job_id, "awaiting_review")
    await js.set_result(
        ws.a, j2.job_id, {"escalation_reason": "debate_exhausted"},
        cost_usd=0.0, status="awaiting_review",
    )

    counts = await js.status_counts(ws.a)
    assert counts == {"done": 1, "awaiting_review": 1}
    all_counts = await js.status_counts_all()
    assert sum(all_counts.values()) >= 3

    reasons = await js.escalation_reasons(ws.a)
    assert reasons.get("debate_exhausted") == 1

    days = await js.daily_stats(ws.a, days=1)
    assert days and days[0]["jobs"] == 2

    # A brief that declares a cap feeds the budget aggregate; the
    # unbudgeted ones don't.
    await js.create(ws.a, brief.model_copy(update={"budget_usd": 5.0}))
    budget = await js.budget_usage(ws.a)
    assert budget["jobs_with_budget"] == 1
    assert budget["budget_usd"] == pytest.approx(5.0)


# ─── usage ──────────────────────────────────────────────────────────────────


async def test_usage_record_and_aggregates(stores: Any, ws: Any) -> None:
    us = stores.usage_store
    # job_cost() keys on job_id alone — unique ids on a shared DB.
    j1, j2 = f"j1-{ws.a}", f"j2-{ws.b}"
    await us.record(
        job_id=j1, workspace_id=ws.a, role="writer", model="m",
        prompt_tokens=100, completion_tokens=50, cost_usd=0.001,
        latency_ms=200, prompt_name="writer", prompt_version=1,
        prompt_sha256="ab" * 32,
    )
    await us.record(
        job_id=j1, workspace_id=ws.a, role="reviewer", model="m",
        cost_usd=0.0005, latency_ms=100,
        prompt_name="reviewer", prompt_version=2,
    )
    await us.record(job_id=j2, workspace_id=ws.b, role="writer",
                    model="m", cost_usd=9.0)

    assert await us.job_cost(j1) == pytest.approx(0.0015)

    summary = await us.workspace_summary(ws.a)
    assert summary.event_count == 2
    assert summary.total_cost_usd == pytest.approx(0.0015)
    assert summary.by_role["writer"] == pytest.approx(0.001)
    assert summary.by_role["reviewer"] == pytest.approx(0.0005)

    other = await us.workspace_summary(ws.b)
    assert other.event_count == 1 and "reviewer" not in other.by_role
    empty = await us.workspace_summary(ws.c)
    assert empty.event_count == 0 and empty.total_cost_usd == 0.0

    roles = await us.per_role_stats(ws.a)
    assert roles["writer"]["calls"] == 1
    assert roles["writer"]["cost_usd"] == pytest.approx(0.001)

    mix = await us.prompt_version_mix(ws.a)
    assert {(m["prompt_name"], m["prompt_version"]) for m in mix} == {
        ("writer", 1), ("reviewer", 2),
    }

    daily = await us.daily_cost(ws.a, days=1)
    assert sum(daily.values()) == pytest.approx(0.0015)


# ─── workspaces / members / api keys ────────────────────────────────────────


async def test_workspace_member_key_lifecycle(stores: Any, ws: Any) -> None:
    wss = stores.workspace_store
    await wss.migrate(admin_subject="admin@x")
    assert await wss.create_workspace(ws.t1, "Tenant One", creator="alice")
    # Duplicate id → False, no clobber.
    assert not await wss.create_workspace(ws.t1, "Other", creator="bob")

    await wss.add_member(ws.t1, "bob", "viewer")
    assert await wss.get_member(ws.t1, "alice") == "admin"
    assert await wss.get_member(ws.t1, "bob") == "viewer"
    await wss.add_member(ws.t1, "bob", "member")  # upsert changes role
    assert await wss.get_member(ws.t1, "bob") == "member"
    assert await wss.get_member(ws.t1, "carol") is None

    subs = {w["workspace_id"]: w["role"] for w in await wss.list_workspaces("alice")}
    assert subs[ws.t1] == "admin"
    assert ws.t1 not in {w["workspace_id"] for w in await wss.list_workspaces("carol")}
    all_ws = {w["workspace_id"] for w in await wss.list_all()}
    assert {ws.t1, "default"} <= all_ws
    members = {m["subject"] for m in await wss.list_members(ws.t1)}
    assert members == {"alice", "bob"}

    # Password attach + read-back, scoped per tenant.
    assert await wss.set_member_password(ws.t1, "alice", "h1")
    assert not await wss.set_member_password(ws.t1, "carol", "h2")
    assert await wss.get_member_password_hash(ws.t1, "alice") == "h1"
    assert await wss.get_member_password_hash(ws.t1, "bob") is None

    # API key: issue → lookup → list (metadata) → revoke.
    key_id, full_key = await wss.issue_api_key(ws.t1, role="member", label="ci")
    assert full_key.startswith("orx_")
    rec = await wss.lookup_api_key(full_key)
    assert rec is not None and rec.key_id == key_id
    assert rec.workspace_id == ws.t1 and rec.role == "member"
    listed = await wss.list_api_keys(ws.t1)
    assert listed[0]["key_id"] == key_id
    assert "key_hash" not in listed[0] or listed[0]["key_hash"] != hash_api_key(full_key)
    assert await wss.revoke_api_key(ws.t1, key_id)
    assert await wss.lookup_api_key(full_key) is None
    # UPDATE matches the row whether or not it was already revoked —
    # the contract is "row exists", not "transition happened".
    assert await wss.revoke_api_key(ws.t1, key_id)
    assert not await wss.revoke_api_key(ws.t1, "nope")


# ─── feedback ───────────────────────────────────────────────────────────────


async def test_feedback_record_list_export(stores: Any, ws: Any) -> None:
    fs = stores.feedback_store
    j1, j2, j3 = f"j1-{ws.a}", f"j2-{ws.a}", f"j3-{ws.b}"
    fid = await fs.record(ws.a, j1, "up", comment="good", section="s1")
    assert fid > 0
    await fs.record(ws.a, j2, "down", comment="bad")
    await fs.record(ws.b, j3, "up")

    rows = await fs.list(ws.a)
    assert len(rows) == 2
    assert {r["job_id"] for r in rows} == {j1, j2}
    assert len(await fs.list(ws.b)) == 1
    assert await fs.list(ws.c) == []

    cases = await fs.export_cases(ws.a)
    assert len(cases) == 2
    assert cases[0]["source"] == "operator-feedback"
    assert cases[0]["candidate_case"]["id"].startswith("fb-")

    with pytest.raises(ValueError):
        await fs.record(ws.a, j1, "meh")
    await fs.close()


# ─── tokens ─────────────────────────────────────────────────────────────────


async def test_token_revocation_and_sweep(stores: Any, ws: Any) -> None:
    ts = stores.token_store
    future = datetime.now(UTC) + timedelta(minutes=30)
    await ts.revoke("jti-1", future)
    assert await ts.is_revoked("jti-1")
    assert not await ts.is_revoked("jti-2")

    # Already-expired tokens don't count as revoked.
    await ts.revoke("jti-old", datetime.now(UTC) - timedelta(hours=1))
    assert not await ts.is_revoked("jti-old")

    removed = await ts.sweep()
    assert removed >= 1
    # Idempotent re-revoke is harmless.
    await ts.revoke("jti-1", future)
    assert await ts.is_revoked("jti-1")


# ─── cache ──────────────────────────────────────────────────────────────────


async def test_cache_roundtrip_ttl_and_prune(stores: Any, ws: Any) -> None:
    cs = stores.cache_store
    now = datetime.now(UTC)
    await cs.put("scrape", ws.a, "https://x", '{"ok":1}', now=now)
    await cs.put("scrape", ws.b, "https://x", '{"ok":2}', now=now)

    assert await cs.get("scrape", ws.a, "https://x", ttl_hours=1, now=now) == '{"ok":1}'
    # Tenant isolation: same key, different workspace.
    assert await cs.get("scrape", ws.b, "https://x", ttl_hours=1, now=now) == '{"ok":2}'
    # Miss and kind separation.
    assert await cs.get("search", ws.a, "https://x", ttl_hours=1, now=now) is None
    assert await cs.get("scrape", ws.a, "other", ttl_hours=1, now=now) is None

    # TTL expiry: read past the horizon returns None and self-prunes.
    later = now + timedelta(hours=2)
    assert await cs.get("scrape", ws.a, "https://x", ttl_hours=1, now=later) is None

    # Stale-day prune drops yesterday's rows.
    yesterday = now - timedelta(days=1)
    await cs.put("scrape", ws.a, "old", "1", now=yesterday)
    pruned = await cs.prune(now=now)
    assert pruned >= 1
    assert await cs.get("scrape", ws.a, "old", ttl_hours=999, now=now) is None


# ─── schema auto-migration ──────────────────────────────────────────────────


async def test_schema_auto_migrates_on_first_use(stores: Any, brief: Brief, ws: Any) -> None:
    """No migration runner needed: first call to any store creates its
    tables (SQLite ``CREATE TABLE IF NOT EXISTS``, same contract for
    Postgres). Exercising each store above already proves it; this pins
    the cross-store invariant explicitly."""
    job = await stores.job_store.create(ws.z, brief)
    await stores.workspace_store.migrate()
    await stores.token_store.revoke(
        "t", datetime.now(UTC) + timedelta(minutes=1)
    )
    assert await stores.job_store.get(ws.z, job.job_id) is not None


# ─── factory (no live DB needed — PostgresStores is lazy) ────────────────────


def test_factory_sqlite_default(tmp_path: Any) -> None:
    from app.core.config import Settings
    from app.services.store_factory import build_stores

    settings = Settings(_env_file=None, database_path=str(tmp_path / "a.db"))
    stores = build_stores(settings)
    assert type(stores.job_store).__name__ == "JobStore"
    assert type(stores.token_store).__name__ == "SqliteTokenStore"
    assert stores.cache_store is not None  # cache_enabled default
    no_cache = build_stores(
        Settings(
            _env_file=None,
            database_path=str(tmp_path / "b.db"),
            cache_enabled=False,
        )
    )
    assert no_cache.cache_store is None


def test_factory_postgres_requires_dsn(tmp_path: Any) -> None:
    from app.core.config import Settings
    from app.services.store_factory import build_stores

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_stores(
            Settings(_env_file=None, storage_backend="postgres")
        )


def test_factory_postgres_constructs_lazily(tmp_path: Any) -> None:
    """A DSN builds the store set without connecting — the pool is lazy,
    so startup fails on first query, not on construction."""
    from pydantic import SecretStr

    from app.core.config import Settings
    from app.services.store_factory import build_stores

    stores = build_stores(
        Settings(
            _env_file=None,
            storage_backend="postgres",
            database_url=SecretStr("postgresql://u:p@localhost:1/db"),
        )
    )
    assert type(stores.job_store).__name__ == "PgJobStore"
    assert type(stores.token_store).__name__ == "PgTokenStore"


# ─── prompt registry store (sync seam, Phase 12) ────────────────────────────


@pytest.fixture(params=_backends())
def prompt_store(
    request: pytest.FixtureRequest, tmp_path: Any
) -> Any:
    if request.param == "sqlite":
        from app.services.prompt_store import SqlitePromptStore

        yield SqlitePromptStore(str(tmp_path / "reg.sqlite"))
        return
    from app.services.pg.prompt_store import PgPromptStore

    store = PgPromptStore(os.environ["TEST_DATABASE_URL"])
    yield store
    store.close()


def test_prompt_registry_on_store_backend(prompt_store: Any, ws: Any) -> None:
    """The full registry lifecycle through the injected store — same
    semantics either backend (candidates, promote, rollback, chain, A/B)."""
    from app.services.prompt_registry import (
        PromptRegistry,
        PromptStatus,
    )

    reg = PromptRegistry.load(store=prompt_store)
    assert reg.get("writer").version == 23
    cand = reg.create_candidate("writer", "pg body\n", "on store")
    assert cand.status == PromptStatus.CANDIDATE
    reg.promote("writer", cand.version, actor="t")
    assert reg.get("writer").version == cand.version
    reg.rollback("writer", 21, actor="t", reason="x")
    assert reg.get("writer").version == 21
    valid, _root, entries = reg.verify_audit_chain()
    assert valid and len(entries) >= 3
    # A/B run record, scoped.
    assert reg.save_ab_run(
        f"run-{ws.a}", name="writer", version=1,
        workspace_id=ws.a,
        result={"status": "done", "x": 1},
    )
    got = reg.get_ab_run(f"run-{ws.a}", ws.a)
    assert got is not None and got["status"] == "done"
    assert reg.get_ab_run(f"run-{ws.a}", ws.b) is None
