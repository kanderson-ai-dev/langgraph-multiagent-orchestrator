"""FastAPI dependencies: workspace scoping, auth, rate limiting.

Every route resolves a ``RequestContext`` — the workspace (tenant) plus the
authenticated subject. When auth is disabled (no JWT secret configured),
everything lands in the ``default`` workspace.
"""

from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimiter
from app.core.security import verify_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class RequestContext:
    workspace_id: str
    subject: str | None
    authenticated: bool


_rate_limiter = RateLimiter(limit=60)


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


async def get_context(
    request: Request,
    settings: Settings = Depends(get_settings),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> RequestContext:
    """Resolve the caller's workspace.

    Auth enabled → require a valid Bearer JWT; its ``workspace_id`` claim
    scopes everything. Auth disabled → open local dev, ``default`` workspace.
    """
    if not settings.auth_enabled:
        return RequestContext("default", subject=None, authenticated=False)
    if creds is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="authentication required")
    payload = verify_token(settings, creds.credentials)
    if payload is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="authentication required")
    return RequestContext(payload.workspace_id, payload.sub, authenticated=True)
