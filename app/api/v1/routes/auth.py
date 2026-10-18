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

# Valid-format bcrypt hash used to burn one compare when a member row or
# its password doesn't exist — failure timing stays uniform.
_DUMMY_BCRYPT_HASH = "$2b$12$LJ3m4yB5kz0v2nwI0p5J9eJ9f0QFm1xRk5gP7y1q2u6k8f3o0b3dS"

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
    # Rate-limit login attempts by client IP *and* by username, so spraying
    # one account from many addresses still trips the limiter.
    client = request.client.host if request.client else "unknown"
    if not await limiter.allow(f"login:{client}") or not await limiter.allow(
        f"login-user:{body.username.lower()}"
    ):
        raise HTTPException(status_code=429, detail="too many requests")

    if not settings.auth_enabled:
        # Open dev mode — no credentials configured.
        raise HTTPException(status_code=503, detail="auth not configured")

    store = getattr(request.app.state, "workspace_store", None)
    admin_ok = (
        body.username == settings.admin_username
        and settings.admin_password_hash is not None
        and verify_password(
            body.password, settings.admin_password_hash.get_secret_value()
        )
    )
    if admin_ok:
        # Role resolution: an explicit membership wins; the platform admin
        # bootstraps as workspace admin everywhere else (pre-RBAC implicit
        # tenants keep working — the migration seals ``default`` formally).
        role = "admin"
        if store is not None:
            member_role = await store.get_member(body.workspace_id, body.username)
            if member_role is not None:
                role = member_role
    else:
        # Workspace-member credentials: each member carries a bcrypt
        # password_hash set by a workspace admin. Unknown member, unset
        # password, and wrong password all burn the same bcrypt compare and
        # collapse to one generic 401 — no user enumeration.
        member_hash = (
            await store.get_member_password_hash(body.workspace_id, body.username)
            if store is not None
            else None
        )
        member_ok = verify_password(
            body.password, member_hash or _DUMMY_BCRYPT_HASH
        ) and member_hash is not None
        if not member_ok:
            raise HTTPException(status_code=401, detail="invalid credentials")
        role = (
            await store.get_member(body.workspace_id, body.username)
            if store is not None
            else None
        ) or "member"

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
