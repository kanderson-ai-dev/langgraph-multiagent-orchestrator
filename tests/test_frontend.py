"""Frontend static mounts — landing at `/`, console at `/console`,
and proof that static mounts never shadow API routes."""

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_landing_served_at_root() -> None:
    r = _client().get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "New research brief" in r.text


def test_console_served_with_trailing_slash() -> None:
    r = _client().get("/console/")
    assert r.status_code == 200
    assert "Live transcript" in r.text


def test_console_assets_served() -> None:
    client = _client()
    assert client.get("/console/styles.css").status_code == 200
    assert client.get("/console/app.js").status_code == 200


def test_api_routes_not_shadowed() -> None:
    """API and health routes must resolve even with a `/` route mounted."""
    client = _client()
    assert client.get("/health/live").json()["status"] == "ok"
    assert client.get("/health/ready").status_code == 200
    r = client.post("/api/v1/orchestration/reports", json={"topic": "ab"})
    assert r.status_code in (401, 422)  # route reached; validation rejects it


def test_configure_tracing_exports_langsmith_env(monkeypatch, tmp_path):
    """LangSmith settings must reach os.environ for LangChain tracers."""
    import os

    from pydantic import SecretStr

    from app.core.config import Settings
    from app.main import configure_tracing

    for k in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(k, raising=False)

    s = Settings(
        openai_api_key=None,
        langchain_tracing_v2=True,
        langchain_api_key=SecretStr("lsv2-test"),
        langchain_project="test-project",
        database_path=str(tmp_path / "db.sqlite"),
    )
    assert configure_tracing(s) is True
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "lsv2-test"
    assert os.environ["LANGCHAIN_PROJECT"] == "test-project"


def test_configure_tracing_disabled_without_key(monkeypatch):
    import os

    from app.core.config import Settings
    from app.main import configure_tracing

    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    s = Settings(langchain_tracing_v2=True, langchain_api_key=None)
    assert configure_tracing(s) is False
    assert "LANGCHAIN_API_KEY" not in os.environ
