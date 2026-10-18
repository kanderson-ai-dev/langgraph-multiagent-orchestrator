"""FastAPI dependencies: workspace scoping, auth, RBAC, rate limiting.

Every route resolves a ``RequestContext`` — the workspace (tenant) plus the
authenticated subject and their role (``viewer``/``member``/``admin``).
When auth is disabled (no JWT secret configured), everything lands in the
``default`` workspace with full ``admin`` rights — local dev stays open.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimiter, build_rate_limiter
from app.core.security import verify_token
from app.services.workspace_store import KEY_PREFIX

if TYPE_CHECKING:
    from app.services.workspace_store import WorkspaceStore

_bearer = HTTPBearer(auto_error=False)

ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}


@dataclass(frozen=True)
class RequestContext:
    workspace_id: str
    subject: str | None
    authenticated: bool
    role: str = "admin"  # open dev defaults to admin; auth always sets it
    via_api_key: bool = False


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Lazily built shared limiter — Redis backend when REDIS_URL is set.

    Built on first request (not import) so tests may override settings
    before the backend is chosen.
    """
    global _limiter
    if _limiter is None:
        _limiter = build_rate_limiter(get_settings())
    return _limiter


async def get_context(
    request: Request,
    settings: Settings = Depends(get_settings),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> RequestContext:
    """Resolve the caller's workspace + role.

    Auth enabled → a Bearer JWT (``workspace_id``/``role`` claims) or a
    workspace API key (``orx_…`` → hash lookup). Auth disabled → open
    local dev in ``default`` with admin rights.
    """
    if not settings.auth_enabled:
        return RequestContext("default", subject=None, authenticated=False)
    if creds is None:
        raise HTTPException(status_code=401, detail="authentication required")

    token = creds.credentials
    if token.startswith(KEY_PREFIX):
        store: WorkspaceStore | None = getattr(
            request.app.state, "workspace_store", None
        )
        record = await store.lookup_api_key(token) if store else None
        if record is None:
            raise HTTPException(
                status_code=401, detail="authentication required"
            )
        return RequestContext(
            record.workspace_id,
            subject=record.key_id,
            authenticated=True,
            role=record.role,
            via_api_key=True,
        )

    payload = verify_token(settings, token)
    if payload is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if payload.jti:
        from app.services.token_store import TokenStore

        token_store: TokenStore | None = getattr(
            request.app.state, "token_store", None
        )
        if token_store is not None and await token_store.is_revoked(
            payload.jti
        ):
            raise HTTPException(
                status_code=401, detail="authentication required"
            )
    return RequestContext(
        payload.workspace_id,
        payload.sub,
        authenticated=True,
        role=payload.role,
    )


def require_role(
    minimum: str,
) -> Callable[[RequestContext], Awaitable[RequestContext]]:
    """Dependency factory: caller's role rank must be >= ``minimum``.

    Rejects with a generic 403 — never distinguishes "no such workspace"
    from "exists but you may not touch it".
    """

    async def dep(
        ctx: RequestContext = Depends(get_context),
    ) -> RequestContext:
        if ROLE_RANK.get(ctx.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=403, detail="insufficient permissions"
            )
        return ctx

    return dep
