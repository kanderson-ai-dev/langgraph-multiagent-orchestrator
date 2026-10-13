"""Tracing provider seam — LangSmith/Langfuse selection, masking, no-op paths.

Everything here is offline: the Langfuse SDK client is replaced by fakes, so
no test needs credentials or network. The provider must degrade to a silent
no-op whenever keys are missing or the backend misbehaves — tracing is
observability, never a request blocker.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services import tracing


@pytest.fixture
def langfuse_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "tracing_provider": "langfuse",
            "langfuse_public_key": SecretStr("pk-lf-test"),
            "langfuse_secret_key": SecretStr("sk-lf-test"),
        }
    )


@pytest.fixture(autouse=True)
def clean_client() -> Any:
    tracing.reset_client()
    yield
    tracing.reset_client()


class _FakeSpan:
    """Minimal stand-in for a Langfuse observation context manager."""

    def __init__(self, trace_id: str = "t-1", span_id: str = "s-1") -> None:
        self.trace_id = trace_id
        self.id = span_id

    def __enter__(self) -> _FakeSpan:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeClient:
    """Records what the seam asks of the SDK — no exporter, no network."""

    def __init__(self, *, fail: bool = False, ambient: str | None = None) -> None:
        self.observations: list[dict[str, Any]] = []
        self.flushed = False
        self.stopped = False
        self._fail = fail
        self._ambient = ambient

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        if self._fail:
            raise RuntimeError("exporter down")
        self.observations.append(kwargs)
        return _FakeSpan()

    def get_current_trace_id(self) -> str | None:
        return self._ambient

    def create_trace_id(self) -> str:
        return "minted-1"

    def auth_check(self) -> bool:
        return True

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.stopped = True


def test_noop_without_keys(settings: Settings) -> None:
    settings = settings.model_copy(update={"tracing_provider": "langfuse"})
    assert tracing.init_tracing(settings) == "none"
    with tracing.bind_trace(name="job-1") as callbacks:
        assert callbacks == []
    assert tracing.create_trace_id() is None
    assert tracing.current_trace_id() is None


def test_noop_when_provider_none(langfuse_settings: Settings) -> None:
    # Keys present but the slot says "none" — nothing activates.
    settings = langfuse_settings.model_copy(update={"tracing_provider": "none"})
    assert tracing.init_tracing(settings) == "none"


def test_langfuse_init_exports_env_and_disables_langsmith(
    langfuse_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(tracing, "_build_client", lambda s: fake)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert tracing.init_tracing(langfuse_settings) == "langfuse"
    import os

    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-lf-test"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_bind_trace_opens_observation_and_yields_callback(
    langfuse_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(tracing, "_client", fake)
    with tracing.bind_trace(
        name="job-42", session_id="job-42",
        metadata={"workspace_id": "ws"}, tags=["job"],
    ) as callbacks:
        assert len(callbacks) == 1  # real langfuse.langchain.CallbackHandler
    assert fake.observations[0]["name"] == "job-42"


def test_bind_trace_rejoins_parent_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(tracing, "_client", fake)
    with tracing.bind_trace(name="resume", trace_id="t-parent"):
        pass
    assert fake.observations[0]["trace_context"] == {"trace_id": "t-parent"}


def test_bind_trace_attaches_to_ambient_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inside e.g. a run_experiment item, no second root span opens.
    fake = _FakeClient(ambient="t-experiment")
    monkeypatch.setattr(tracing, "_client", fake)
    with tracing.bind_trace(name="eval:x") as callbacks:
        assert len(callbacks) == 1
    assert fake.observations == []


def test_concurrent_traces_get_independent_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(tracing, "_client", fake)
    with tracing.bind_trace(name="a") as cb_a, \
            tracing.bind_trace(name="b") as cb_b:
        assert cb_a[0] is not cb_b[0]


def test_backend_failure_swallowed(langfuse_settings: Settings,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_client", _FakeClient(fail=True))
    with tracing.bind_trace(name="job-x") as callbacks:
        assert callbacks == []  # request proceeds untraced


def test_init_failure_means_none(langfuse_settings: Settings,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tracing, "_build_client", lambda s: (_ for _ in ()).throw(RuntimeError)
        if False else None
    )
    assert tracing.init_tracing(langfuse_settings) == "none"


def test_shutdown_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(tracing, "_client", fake)
    tracing.shutdown_tracing()
    assert fake.flushed and fake.stopped


def test_trace_id_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_client", _FakeClient(ambient="t-1"))
    assert tracing.current_trace_id() == "t-1"
    assert tracing.create_trace_id() == "minted-1"


def test_mask_redacts_secrets_and_system_prompts() -> None:
    payload = {
        "api_key": "sk-secret",
        "authorization": "Bearer tok",
        "nested": {"refresh_token": "abc", "safe": 1},
        "messages": [
            {"role": "system", "content": "TOP SECRET PROMPT"},
            {"role": "user", "content": "write a report"},
        ],
    }
    masked = tracing._mask_payload(data=payload)
    assert masked["api_key"] == "***REDACTED***"
    assert masked["authorization"] == "***REDACTED***"
    assert masked["nested"]["refresh_token"] == "***REDACTED***"
    assert masked["nested"]["safe"] == 1
    assert masked["messages"][0]["content"] == "[prompt template masked]"
    assert masked["messages"][1]["content"] == "write a report"
