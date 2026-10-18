"""Settings: secrets normalize empty strings to None and degrade cleanly."""

import pytest

from app.core.config import Settings


def test_empty_string_secrets_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("SEARCH_API_KEY", "   ")
    monkeypatch.setenv("JWT_SECRET_KEY", "")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    s = Settings(_env_file=None)
    assert s.openai_api_key is None
    assert s.search_api_key is None
    assert s.jwt_secret_key is None
    assert s.langfuse_public_key is None
    assert s.langfuse_secret_key is None


def test_secrets_never_serialize_plain() -> None:
    """SecretStr repr/dump must not leak values — covers Langfuse keys."""
    s = Settings(
        _env_file=None,
        langfuse_public_key="pk-lf-live",
        langfuse_secret_key="sk-lf-live",
    )
    for blob in (repr(s), str(s.model_dump()), repr(s.model_dump())):
        assert "pk-lf-live" not in blob
        assert "sk-lf-live" not in blob
    assert s.tracing_provider == "none"  # keys alone never enable tracing


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


def test_supervisor_dispatch_bound_configurable() -> None:
    s = Settings(_env_file=None, supervisor_max_dispatches=7)
    assert s.supervisor_max_dispatches == 7
    assert Settings(_env_file=None).supervisor_max_dispatches == 25
    with pytest.raises(ValueError):
        Settings(_env_file=None, supervisor_max_dispatches=0)


def test_eval_timeout_configurable() -> None:
    s = Settings(_env_file=None, prompt_eval_timeout_seconds=5.0)
    assert s.prompt_eval_timeout_seconds == 5.0
    with pytest.raises(ValueError):
        Settings(_env_file=None, prompt_eval_timeout_seconds=0)


def test_graph_uses_dispatch_setting(settings: Settings) -> None:
    """build_graph must thread supervisor_max_dispatches into the Supervisor."""
    from app.graph.graph import build_graph
    from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm

    settings.supervisor_max_dispatches = 4
    graph = build_graph(
        settings,
        llm=_stub_llm(),
        search=_FakeSearch(),
        scraper=_FakeScraper(),
        checkpointer=None,
    )
    assert graph is not None


def test_reviewer_threshold_is_the_shared_constant() -> None:
    """The Reviewer's citation check and the EDD gate share one number."""
    from app.core.thresholds import EDD_THRESHOLDS
    from app.graph.nodes.reviewer import _SUPPORT_THRESHOLD
    from app.services.prompt_eval import EDD_THRESHOLDS as PEV

    assert EDD_THRESHOLDS["citation_support_rate"] == _SUPPORT_THRESHOLD
    assert PEV is EDD_THRESHOLDS  # prompt_eval re-exports, never duplicates
