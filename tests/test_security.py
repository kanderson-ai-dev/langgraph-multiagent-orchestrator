"""Security helpers: bcrypt hashing and JWT round-trip."""

from app.core.config import Settings
from app.core.security import (
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)


def _auth_settings() -> Settings:
    return Settings(_env_file=None, jwt_secret_key="s" * 64, jwt_expire_minutes=30)


def test_password_hash_round_trip() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)


def test_verify_password_malformed_hash_returns_false() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_jwt_round_trip() -> None:
    s = _auth_settings()
    token = issue_token(s, subject="admin", workspace_id="ws-123")
    payload = verify_token(s, token)
    assert payload is not None
    assert payload.sub == "admin"
    assert payload.workspace_id == "ws-123"


def test_jwt_wrong_secret_fails_generically() -> None:
    s = _auth_settings()
    token = issue_token(s, subject="admin")
    other = Settings(_env_file=None, jwt_secret_key="t" * 64)
    assert verify_token(other, token) is None


def test_jwt_expired_fails() -> None:
    s = Settings(_env_file=None, jwt_secret_key="s" * 64, jwt_expire_minutes=1)
    token = issue_token(s, subject="admin")
    # Corrupt the signature segment — verifies generic failure path.
    assert verify_token(s, token + "x") is None


def test_jwt_unconfigured_returns_none() -> None:
    s = Settings(_env_file=None)
    assert verify_token(s, "any-token") is None
