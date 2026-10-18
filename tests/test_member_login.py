"""Workspace-member credentials: per-member passwords on the login path.

Members get their own bcrypt password (set by a workspace admin) instead of
sharing the platform admin's credentials. Failures stay generic — unknown
user, unset password, and wrong password all answer the same 401 — and a
per-username rate limiter complements the per-IP one.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.dependencies import get_rate_limiter
from app.core.rate_limit import InMemoryRateLimiter
from app.core.security import hash_password
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("adminpw"),
        jwt_expire_minutes=30,
    )


@pytest.fixture()
def client(tmp_path: Any) -> Any:
    settings = _settings()
    graph = build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=None,
    )
    db = str(tmp_path / "members.sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    limiter = InMemoryRateLimiter(limit=60, window_seconds=60)
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    with TestClient(app) as c:
        yield c


def _admin_headers(client: Any, ws: str = "default") -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminpw", "workspace_id": ws},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _enroll_member(client: Any, ws: str, subject: str, password: str,
                   role: str = "member") -> dict[str, str]:
    h = _admin_headers(client)
    r = client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"subject": subject, "role": role},
        headers=h,
    )
    assert r.status_code == 200
    r = client.put(
        f"/api/v1/workspaces/{ws}/members/{subject}/password",
        json={"password": password},
        headers=h,
    )
    assert r.status_code == 200
    return h


def test_member_login_with_own_password(client: Any) -> None:
    _enroll_member(client, "default", "alice", "alice-secret-1")
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "alice-secret-1"},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    me = client.get(
        "/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 200
    assert {w["workspace_id"] for w in me.json()} == {"default"}


def test_member_role_is_scoped_to_workspace(client: Any) -> None:
    _enroll_member(client, "default", "bob", "bob-secret-123", role="viewer")
    token = client.post(
        "/api/v1/auth/login",
        json={"username": "bob", "password": "bob-secret-123"},
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    # viewer can read but cannot submit a job (requires member).
    assert client.get("/api/v1/workspaces", headers=h).status_code == 200
    r = client.post(
        "/api/v1/orchestration/reports", json={"topic": "x"}, headers=h
    )
    assert r.status_code == 403


def test_wrong_member_password_is_generic_401(client: Any) -> None:
    _enroll_member(client, "default", "carol", "carol-secret")
    bad = client.post(
        "/api/v1/auth/login",
        json={"username": "carol", "password": "wrong-password"},
    )
    missing = client.post(
        "/api/v1/auth/login",
        json={"username": "nobody", "password": "wrong-password"},
    )
    assert bad.status_code == missing.status_code == 401
    assert bad.json() == missing.json() == {"detail": "invalid credentials"}


def test_member_credentials_do_not_cross_workspaces(client: Any) -> None:
    h = _admin_headers(client)
    client.post(
        "/api/v1/workspaces", json={"id": "ws-b", "name": "B"}, headers=h
    )
    _enroll_member(client, "default", "dave", "dave-secret-9")
    # dave has no membership (or password) in ws-b → generic 401.
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "dave", "password": "dave-secret-9",
              "workspace_id": "ws-b"},
    )
    assert r.status_code == 401


def test_password_set_requires_admin_and_member(client: Any) -> None:
    # Nonexistent member → 404.
    h = _admin_headers(client)
    r = client.put(
        "/api/v1/workspaces/default/members/ghost/password",
        json={"password": "ghost-secret"},
        headers=h,
    )
    assert r.status_code == 404
    # A plain member cannot set passwords.
    _enroll_member(client, "default", "erin", "erin-secret-1")
    member_token = client.post(
        "/api/v1/auth/login",
        json={"username": "erin", "password": "erin-secret-1"},
    ).json()["access_token"]
    r = client.put(
        "/api/v1/workspaces/default/members/erin/password",
        json={"password": "erin-new-pass"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code in (403, 404)


def test_readding_member_preserves_password(client: Any) -> None:
    _enroll_member(client, "default", "fran", "fran-secret-7")
    # Re-add with a different role — the UPSERT must not wipe the hash.
    client.post(
        "/api/v1/workspaces/default/members",
        json={"subject": "fran", "role": "viewer"},
        headers=_admin_headers(client),
    )
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "fran", "password": "fran-secret-7"},
    )
    assert r.status_code == 200


def test_login_rate_limited_per_username(client: Any) -> None:
    # The per-username limiter shares the same backing limiter; hammering
    # one username past the budget trips 429 regardless of outcome.
    for _ in range(60):
        client.post(
            "/api/v1/auth/login",
            json={"username": "spray", "password": "nope-nope-nope"},
        )
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "spray", "password": "nope-nope-nope"},
    )
    assert r.status_code == 429
