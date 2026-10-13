"""RBAC — role × endpoint matrix, workspace API keys, tenant isolation.

Covers the Phase-10 contract: JWT roles gate route classes (reads
``viewer+``, job submission/review ``member+``, prompt mutations
``admin+``), per-workspace API keys authenticate with their stored role,
and every route answers cross-tenant access with the same generic
404/403 — never distinguishing "exists but forbidden".
"""

import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.security import hash_password, issue_token
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from app.services.workspace_store import WorkspaceStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm

V = "/api/v1"


@pytest.fixture()
def auth_app() -> Any:
    """Auth-enabled app with a real workspace store + migrated default."""
    settings = Settings(
        _env_file=None,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = tempfile.mktemp(suffix=".sqlite")
    graph = build_graph(
        settings,
        llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=None,
    )
    store = WorkspaceStore(db)
    app.state.workspace_store = store
    app.state.runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.job_store = JobStore(db)
    app.state.usage_store = UsageStore(db)
    app.state.test_settings = settings
    return app


def _tok(settings: Settings, ws: str, role: str, sub: str = "admin") -> str:
    return issue_token(settings, subject=sub, workspace_id=ws, role=role)


def _h(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ─── role × endpoint matrix ─────────────────────────────────────────────────


@pytest.mark.parametrize("role,expected", [("viewer", 403), ("member", 202), ("admin", 202)])
def test_submit_requires_member(auth_app: Any, role: str, expected: int) -> None:
    s = auth_app.state.test_settings
    with TestClient(auth_app) as c:
        r = c.post(
            f"{V}/orchestration/reports",
            json={"topic": "quarterly security posture"},
            headers=_h(_tok(s, "ws-rbac", role)),
        )
    assert r.status_code == expected


@pytest.mark.parametrize("role,expected", [("viewer", 200), ("member", 200), ("admin", 200)])
def test_reads_allow_viewer(auth_app: Any, role: str, expected: int) -> None:
    s = auth_app.state.test_settings
    with TestClient(auth_app) as c:
        r = c.get(f"{V}/orchestration/reports", headers=_h(_tok(s, "w", role)))
        assert r.status_code == expected
        r2 = c.get(f"{V}/prompts", headers=_h(_tok(s, "w", role)))
        assert r2.status_code == expected
        r3 = c.get(f"{V}/dashboard/summary", headers=_h(_tok(s, "w", role)))
        assert r3.status_code == expected


@pytest.mark.parametrize("role,expected", [("viewer", 403), ("member", 403), ("admin", 201)])
def test_prompt_mutation_requires_admin(
    auth_app: Any, role: str, expected: int
) -> None:
    s = auth_app.state.test_settings
    with TestClient(auth_app) as c:
        # The writer prompt declares no template variables — a candidate
        # must keep that exact placeholder contract.
        r = c.post(
            f"{V}/prompts/writer/candidates",
            json={
                "template": "Draft the report strictly from the evidence.",
                "changelog": "rbac test",
            },
            headers=_h(_tok(s, "w", role)),
        )
    assert r.status_code == expected


def test_review_requires_member(auth_app: Any) -> None:
    s = auth_app.state.test_settings
    with TestClient(auth_app) as c:
        r = c.post(
            f"{V}/orchestration/reports/x/review",
            json={"action": "approve"},
            headers=_h(_tok(s, "w", "viewer")),
        )
    assert r.status_code == 403


def test_no_token_401(auth_app: Any) -> None:
    with TestClient(auth_app) as c:
        assert c.get(f"{V}/orchestration/reports").status_code == 401
        assert c.post(f"{V}/workspaces", json={"id": "x", "name": "X"}).status_code == 401


# ─── workspace administration ────────────────────────────────────────────────


def test_workspace_crud_and_members(auth_app: Any) -> None:
    s = auth_app.state.test_settings
    admin = _h(_tok(s, "default", "admin"))
    with TestClient(auth_app) as c:
        r = c.post(f"{V}/workspaces", json={"id": "acme", "name": "Acme"}, headers=admin)
        assert r.status_code == 201

        r = c.post(
            f"{V}/workspaces/acme/members",
            json={"subject": "analyst-1", "role": "member"},
            headers=admin,
        )
        assert r.status_code == 200

        r = c.get(f"{V}/workspaces/acme/members", headers=admin)
        assert any(m["subject"] == "analyst-1" for m in r.json())

        # Duplicate workspace id → 409.
        r = c.post(f"{V}/workspaces", json={"id": "acme", "name": "Again"}, headers=admin)
        assert r.status_code == 409


def test_member_role_overrides_login_bootstrap(auth_app: Any) -> None:
    """An explicit membership wins over the platform-admin bootstrap."""
    s = auth_app.state.test_settings
    admin = _h(_tok(s, "default", "admin"))
    with TestClient(auth_app) as c:
        c.post(f"{V}/workspaces", json={"id": "acme", "name": "Acme"}, headers=admin)
        c.post(
            f"{V}/workspaces/acme/members",
            json={"subject": "admin", "role": "viewer"},
            headers=admin,
        )
        # Login as admin INTO acme → membership gives viewer, not admin.
        r = c.post(
            f"{V}/auth/login",
            json={"username": "admin", "password": "pw", "workspace_id": "acme"},
        )
        tok = r.json()["access_token"]
        r = c.post(
            f"{V}/orchestration/reports",
            json={"topic": "should be forbidden"},
            headers=_h(tok),
        )
        assert r.status_code == 403


# ─── API keys ────────────────────────────────────────────────────────────────


def test_api_key_lifecycle(auth_app: Any) -> None:
    s = auth_app.state.test_settings
    admin = _h(_tok(s, "default", "admin"))
    with TestClient(auth_app) as c:
        r = c.post(
            f"{V}/workspaces/acme/api-keys",
            json={"role": "member", "label": "ci"},
            headers=admin,
        )
        assert r.status_code == 201
        key = r.json()["api_key"]
        assert key.startswith("orx_")
        key_id = r.json()["key_id"]

        # The key authenticates with its own workspace + role.
        r = c.post(
            f"{V}/orchestration/reports",
            json={"topic": "api key submission"},
            headers=_h(key),
        )
        assert r.status_code == 202

        # Listing shows metadata only — never the key or its hash.
        listing = c.get(f"{V}/workspaces/acme/api-keys", headers=admin).json()
        assert listing[0]["prefix"].startswith("orx_")
        assert "api_key" not in listing[0] and "key_hash" not in listing[0]

        # Revoked keys stop authenticating.
        c.delete(f"{V}/workspaces/acme/api-keys/{key_id}", headers=admin)
        r = c.get(f"{V}/orchestration/reports", headers=_h(key))
        assert r.status_code == 401


def test_viewer_api_key_cannot_submit(auth_app: Any) -> None:
    s = auth_app.state.test_settings
    admin = _h(_tok(s, "default", "admin"))
    with TestClient(auth_app) as c:
        key = c.post(
            f"{V}/workspaces/acme/api-keys",
            json={"role": "viewer"},
            headers=admin,
        ).json()["api_key"]
        r = c.post(
            f"{V}/orchestration/reports",
            json={"topic": "denied"},
            headers=_h(key),
        )
        assert r.status_code == 403
        assert c.get(f"{V}/orchestration/reports", headers=_h(key)).status_code == 200


# ─── cross-tenant isolation ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("get", "/workspaces/acme/members", None),
        ("get", "/workspaces/acme/api-keys", None),
        ("post", "/workspaces/acme/members",
         {"subject": "x", "role": "viewer"}),
        ("post", "/workspaces/acme/api-keys", {"role": "member"}),
    ],
)
def test_cross_tenant_generic_denials(
    auth_app: Any, method: str, path: str, json_body: Any
) -> None:
    """A ws-b subject probing acme admin surfaces gets a generic 404 —
    identical to a nonexistent workspace (no existence leakage)."""
    s = auth_app.state.test_settings
    outsider = _h(_tok(s, "ws-b", "admin", sub="outsider"))
    with TestClient(auth_app) as c:
        kwargs = {"json": json_body} if json_body is not None else {}
        r = getattr(c, method)(f"{V}{path}", headers=outsider, **kwargs)
        assert r.status_code == 404
        assert r.json()["detail"] == "workspace not found"


def test_workspace_member_without_admin_gets_403(auth_app: Any) -> None:
    """A member of acme without admin rank gets a generic 403 on admin ops."""
    s = auth_app.state.test_settings
    admin = _h(_tok(s, "default", "admin"))
    with TestClient(auth_app) as c:
        c.post(f"{V}/workspaces", json={"id": "acme2", "name": "A2"}, headers=admin)
        c.post(
            f"{V}/workspaces/acme2/members",
            json={"subject": "member-1", "role": "member"},
            headers=admin,
        )
        member = _h(_tok(s, "acme2", "member", sub="member-1"))
        r = c.post(
            f"{V}/workspaces/acme2/members",
            json={"subject": "y", "role": "viewer"},
            headers=member,
        )
        assert r.status_code == 403
        assert r.json()["detail"] == "insufficient permissions"


def test_job_invisible_across_tenants(auth_app: Any) -> None:
    """A job created in acme is a 404 for ws-b credentials."""
    s = auth_app.state.test_settings
    acme_admin = _h(_tok(s, "acme", "admin"))
    with TestClient(auth_app) as c:
        r = c.post(
            f"{V}/orchestration/reports",
            json={"topic": "acme internal report"},
            headers=acme_admin,
        )
        job_id = r.json()["job_id"]
        outsider = _h(_tok(s, "ws-b", "admin", sub="outsider"))
        r = c.get(f"{V}/orchestration/reports/{job_id}", headers=outsider)
        assert r.status_code == 404
        assert r.json()["detail"] == "job not found"


def test_api_key_cannot_admin_other_workspace(auth_app: Any) -> None:
    s = auth_app.state.test_settings
    admin = _h(_tok(s, "default", "admin"))
    with TestClient(auth_app) as c:
        for ws in ("acme", "beta"):
            c.post(f"{V}/workspaces", json={"id": ws, "name": ws}, headers=admin)
        key = c.post(
            f"{V}/workspaces/acme/api-keys", json={"role": "admin"}, headers=admin
        ).json()["api_key"]
        r = c.post(
            f"{V}/workspaces/beta/members",
            json={"subject": "x", "role": "viewer"},
            headers=_h(key),
        )
        assert r.status_code == 404
