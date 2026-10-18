"""Session security: JWT ``jti``, server-side logout/revocation, CSP headers.

Covers the session half of the auth posture: tokens carry a unique id,
``POST /auth/logout`` revokes them server-side so they stop working before
natural expiry, and every surface serves a Content-Security-Policy that
matches what it actually loads.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import hash_password, issue_token, verify_token
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _auth_settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
        jwt_expire_minutes=30,
    )


@pytest.fixture()
def auth_client(tmp_path: Any) -> Any:
    """TestClient against an auth-enabled app, fully offline."""
    settings = _auth_settings()
    graph = build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=None,
    )
    db = str(tmp_path / "auth.sqlite")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    with TestClient(app) as client:
        yield client


def _login(client: Any) -> str:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "pw", "workspace_id": "default"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def test_issued_tokens_carry_unique_jti() -> None:
    s = _auth_settings()
    a = issue_token(s, subject="admin")
    b = issue_token(s, subject="admin")
    pa, pb = verify_token(s, a), verify_token(s, b)
    assert pa is not None and pb is not None
    assert pa.jti and pb.jti and pa.jti != pb.jti


def test_logout_revokes_jwt(auth_client: Any) -> None:
    token = _login(auth_client)
    headers = {"Authorization": f"Bearer {token}"}
    assert auth_client.get("/api/v1/workspaces", headers=headers).status_code == 200

    r = auth_client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 200 and r.json() == {"revoked": True}

    # The same bearer token must no longer authenticate.
    assert auth_client.get("/api/v1/workspaces", headers=headers).status_code == 401


def test_fresh_login_after_logout_still_works(auth_client: Any) -> None:
    token = _login(auth_client)
    auth_client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    token2 = _login(auth_client)
    assert (
        auth_client.get(
            "/api/v1/workspaces", headers={"Authorization": f"Bearer {token2}"}
        ).status_code
        == 200
    )


def test_logout_is_idempotent_and_quiet(auth_client: Any) -> None:
    # No credentials → still 200, nothing leaked.
    assert auth_client.post("/api/v1/auth/logout").json() == {"revoked": False}
    # Garbage token → same response.
    r = auth_client.post(
        "/api/v1/auth/logout", headers={"Authorization": "Bearer nonsense"}
    )
    assert r.json() == {"revoked": False}
    # A workspace API key isn't a JWT — logout reports nothing revoked.
    r = auth_client.post(
        "/api/v1/auth/logout", headers={"Authorization": "Bearer orx_testkey"}
    )
    assert r.json() == {"revoked": False}


def test_revoked_jti_does_not_block_other_subjects(auth_client: Any) -> None:
    """Revocation is per-token: another session for the same user survives."""
    t1 = _login(auth_client)
    t2 = _login(auth_client)
    auth_client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {t1}"}
    )
    assert (
        auth_client.get(
            "/api/v1/workspaces", headers={"Authorization": f"Bearer {t2}"}
        ).status_code
        == 200
    )


# ─── Content-Security-Policy ────────────────────────────────────────────


def test_csp_console_is_strict(auth_client: Any) -> None:
    csp = auth_client.get("/console/").headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp  # self-hosted modules only
    assert "unsafe-eval" not in csp
    assert "cdn" not in csp


def test_csp_landing_allows_tailwind_and_fonts(auth_client: Any) -> None:
    csp = auth_client.get("/").headers["content-security-policy"]
    assert "https://cdn.tailwindcss.com" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_docs_allows_jsdelivr(auth_client: Any) -> None:
    csp = auth_client.get("/docs").headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp


def test_csp_api_disallows_rendering(auth_client: Any) -> None:
    r = auth_client.get("/status")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
