"""Security helpers: JWT issuance/verification and bcrypt password hashing."""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from pydantic import BaseModel

from app.core.config import Settings


class TokenPayload(BaseModel):
    """Claims embedded in issued JWTs."""

    sub: str
    workspace_id: str
    exp: datetime
    iat: datetime


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verification; never raises on malformed hashes."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def issue_token(
    settings: Settings, *, subject: str, workspace_id: str = "default"
) -> str:
    """Issue a short-lived JWT. Requires ``jwt_secret_key`` to be configured."""
    if settings.jwt_secret_key is None:
        msg = "JWT secret is not configured"
        raise RuntimeError(msg)
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {
            "sub": subject,
            "workspace_id": workspace_id,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def verify_token(settings: Settings, token: str) -> TokenPayload | None:
    """Verify a JWT; returns ``None`` on any failure (generic, no leakage)."""
    if settings.jwt_secret_key is None:
        return None
    try:
        data = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
        return TokenPayload.model_validate(data)
    except (jwt.PyJWTError, ValueError):
        return None
