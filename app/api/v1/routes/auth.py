"""Auth router — rate-limited JWT issuance with generic 401s."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.dependencies import get_rate_limiter
from app.core.rate_limit import RateLimiter
from app.core.security import issue_token, verify_password

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
    if not limiter.allow(f"login:{client}"):
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

    token = issue_token(
        settings, subject=body.username, workspace_id=body.workspace_id
    )
    return TokenResponse(
        access_token=token, expires_in=settings.jwt_expire_minutes * 60
    )
