"""Settings: secrets normalize empty strings to None and degrade cleanly."""

import pytest

from app.core.config import Settings


def test_empty_string_secrets_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("SEARCH_API_KEY", "   ")
    monkeypatch.setenv("JWT_SECRET_KEY", "")
    s = Settings(_env_file=None)
    assert s.openai_api_key is None
    assert s.search_api_key is None
    assert s.jwt_secret_key is None


def test_auth_disabled_without_credentials() -> None:
    s = Settings(_env_file=None)
    assert s.auth_enabled is False


def test_auth_enabled_with_secret_and_hash() -> None:
    s = Settings(
        _env_file=None,
        jwt_secret_key="x" * 32,
        admin_password_hash="$2b$12$fakehashvalue",
    )
    assert s.auth_enabled is True


def test_search_provider_auto_resolution() -> None:
    no_key = Settings(_env_file=None, search_provider="auto")
    assert no_key.effective_search_provider == "duckduckgo"

    with_key = Settings(_env_file=None, search_provider="auto", search_api_key="tvly-x")
    assert with_key.effective_search_provider == "tavily"

    explicit = Settings(_env_file=None, search_provider="none", search_api_key="tvly-x")
    assert explicit.effective_search_provider == "none"


def test_debate_round_bounds() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_debate_rounds=-1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_debate_rounds=99)
