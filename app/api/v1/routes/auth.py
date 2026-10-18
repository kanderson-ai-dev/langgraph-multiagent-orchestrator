"""Auth router — rate-limited JWT issuance with generic 401s."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.dependencies import get_rate_limiter
from app.core.rate_limit import RateLimiter
from app.core.security import issue_token, verify_password, verify_token
from app.services.workspace_store import KEY_PREFIX

_bearer = HTTPBearer(auto_error=False)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    workspace_id: str = "default"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenResponse:
    # Rate-limit login attempts by client IP.
    client = request.client.host if request.client else "unknown"
    if not await limiter.allow(f"login:{client}"):
        raise HTTPException(status_code=429, detail="too many requests")

    if not settings.auth_enabled:
        # Open dev mode — no credentials configured.
        raise HTTPException(status_code=503, detail="auth not configured")

    ok = (
        body.username == settings.admin_username
        and settings.admin_password_hash is not None
        and verify_password(
            body.password, settings.admin_password_hash.get_secret_value()
        )
    )
    if not ok:
        # Generic failure — never distinguish bad user vs bad password.
        raise HTTPException(status_code=401, detail="invalid credentials")

    # Role resolution: an explicit membership wins; the platform admin
    # bootstraps as workspace admin everywhere else (pre-RBAC implicit
    # tenants keep working — the migration seals ``default`` formally).
    role = "admin"
    store = getattr(request.app.state, "workspace_store", None)
    if store is not None:
        member_role = await store.get_member(body.workspace_id, body.username)
        if member_role is not None:
            role = member_role

    token = issue_token(
        settings,
        subject=body.username,
        workspace_id=body.workspace_id,
        role=role,
    )
    return TokenResponse(
        access_token=token, expires_in=settings.jwt_expire_minutes * 60
    )


@router.post("/logout", response_model=dict[str, bool])
async def logout(
    request: Request,
    settings: Settings = Depends(get_settings),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, bool]:
    """Revoke the caller's JWT server-side (its ``jti`` until ``exp``).

    Idempotent and quiet: unauthenticated or malformed calls still get a
    200 — sign-out must never leak whether the token was valid. Workspace
    API keys (``orx_…``) aren't JWTs; their revocation lives on the
    workspace routes, so they just get ``revoked: false``.
    """
    if not settings.auth_enabled or creds is None:
        return {"revoked": False}
    token = creds.credentials
    if token.startswith(KEY_PREFIX):
        return {"revoked": False}
    payload = verify_token(settings, token)
    if payload is None or not payload.jti:
        return {"revoked": False}
    store = getattr(request.app.state, "token_store", None)
    if store is None:
        return {"revoked": False}
    await store.revoke(payload.jti, payload.exp)
    return {"revoked": True}
