"""Workspace administration — tenants, memberships, API-key issuance.

Access model: ``admin`` rank in the *caller token* may create workspaces;
per-workspace routes additionally require that the caller is a member of
the target workspace (or holds the platform-admin subject). Unknown and
forbidden workspaces collapse to the same generic 404 — no existence
leakage across tenants.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.dependencies import RequestContext, require_role
from app.services.workspace_store import ROLES, WorkspaceStore

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _store(request: Request) -> WorkspaceStore:
    store: WorkspaceStore | None = getattr(
        request.app.state, "workspace_store", None
    )
    if store is None:
        raise HTTPException(status_code=503, detail="workspaces unavailable")
    return store


async def _require_ws_admin(
    store: WorkspaceStore,
    ctx: RequestContext,
    workspace_id: str,
    admin_subject: str | None,
) -> None:
    """Caller must administer ``workspace_id``.

    - API keys: bound to their own workspace; anything else → 404.
    - JWT subjects: explicit ``admin`` membership wins; with no membership
      record only the *platform admin subject* bootstraps (matching the
      login resolution) — a workspace-scoped admin token never crosses
      into a foreign workspace.
    - Open dev (no subject) may administer everything.
    Forbidden and missing both answer 404 — no existence leakage.
    """
    if ctx.subject is None:
        return
    if ctx.via_api_key:
        if ctx.workspace_id == workspace_id and ctx.role == "admin":
            return
        raise HTTPException(status_code=404, detail="workspace not found")
    role = await store.get_member(workspace_id, ctx.subject)
    if role == "admin" or (
        role is None
        and ctx.role == "admin"
        and ctx.subject == admin_subject
    ):
        return
    raise HTTPException(
        status_code=403 if role is not None else 404,
        detail="insufficient permissions" if role else "workspace not found",
    )


class WorkspaceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    name: str = Field(min_length=1, max_length=128)


class MemberAdd(BaseModel):
    subject: str = Field(min_length=1, max_length=128)
    role: str


class MemberPassword(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class ApiKeyCreate(BaseModel):
    role: str = "member"
    label: str = Field(default="", max_length=128)


@router.post("", status_code=201, response_model=dict[str, Any])
async def create_workspace(
    body: WorkspaceCreate,
    request: Request,
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, Any]:
    store = _store(request)
    created = await store.create_workspace(body.id, body.name, ctx.subject)
    if not created:
        raise HTTPException(status_code=409, detail="workspace already exists")
    return {"workspace_id": body.id, "name": body.name}


@router.get("", response_model=list[dict[str, Any]])
async def list_workspaces(
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> list[dict[str, Any]]:
    store = _store(request)
    if ctx.subject is None:
        # Open dev: expose every workspace.
        return [
            {"workspace_id": w["workspace_id"], "name": w["name"],
             "role": "admin"}
            for w in await store.list_all()
        ]
    return await store.list_workspaces(ctx.subject)


@router.get("/{workspace_id}/members", response_model=list[dict[str, Any]])
async def list_members(
    workspace_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> list[dict[str, Any]]:
    store = _store(request)
    if ctx.subject is not None:
        role = await store.get_member(workspace_id, ctx.subject)
        if role is None:
            raise HTTPException(status_code=404, detail="workspace not found")
    return await store.list_members(workspace_id)


@router.post("/{workspace_id}/members", response_model=dict[str, str])
async def add_member(
    workspace_id: str,
    body: MemberAdd,
    request: Request,
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, str]:
    if body.role not in ROLES:
        raise HTTPException(status_code=422, detail="invalid role")
    store = _store(request)
    await _require_ws_admin(store, ctx, workspace_id, settings.admin_username)
    await store.add_member(workspace_id, body.subject, body.role)
    return {"workspace_id": workspace_id, "subject": body.subject,
            "role": body.role}


@router.put("/{workspace_id}/members/{subject}/password",
            response_model=dict[str, str])
async def set_member_password(
    workspace_id: str,
    subject: str,
    body: MemberPassword,
    request: Request,
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, str]:
    """Set or rotate a member's login password (bcrypt-hashed at rest)."""
    from app.core.security import hash_password

    store = _store(request)
    await _require_ws_admin(store, ctx, workspace_id, settings.admin_username)
    updated = await store.set_member_password(
        workspace_id, subject, hash_password(body.password)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="member not found")
    return {"workspace_id": workspace_id, "subject": subject, "password_set": "true"}


@router.post("/{workspace_id}/api-keys", status_code=201,
             response_model=dict[str, Any])
async def issue_api_key(
    workspace_id: str,
    body: ApiKeyCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, Any]:
    if body.role not in ROLES:
        raise HTTPException(status_code=422, detail="invalid role")
    store = _store(request)
    await _require_ws_admin(store, ctx, workspace_id, settings.admin_username)
    key_id, full_key = await store.issue_api_key(
        workspace_id, role=body.role, label=body.label
    )
    # The full key is returned exactly once — only its hash persists.
    return {
        "key_id": key_id,
        "api_key": full_key,
        "workspace_id": workspace_id,
        "role": body.role,
    }


@router.get("/{workspace_id}/api-keys", response_model=list[dict[str, Any]])
async def list_api_keys(
    workspace_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(require_role("admin")),
) -> list[dict[str, Any]]:
    store = _store(request)
    await _require_ws_admin(store, ctx, workspace_id, settings.admin_username)
    return await store.list_api_keys(workspace_id)


@router.delete("/{workspace_id}/api-keys/{key_id}",
               response_model=dict[str, str])
async def revoke_api_key(
    workspace_id: str,
    key_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, str]:
    store = _store(request)
    await _require_ws_admin(store, ctx, workspace_id, settings.admin_username)
    revoked = await store.revoke_api_key(workspace_id, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="key not found")
    return {"key_id": key_id, "revoked": "true"}
