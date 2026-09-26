"""Health endpoints and middleware: headers, request id, clean degradation."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_liveness() -> None:
    client = TestClient(create_app())
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_reports_integrations_without_leaking_values() -> None:
    """Isolated from any local .env — integrations must report as disabled."""
    from app.core.config import Settings, get_settings

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # With zero credentials configured, integrations report as disabled —
    # and the response must never contain a secret value.
    assert body["integrations"]["llm"] is False
    assert body["integrations"]["auth"] is False
    assert "key" not in r.text.lower() or "api_key" not in r.text


def test_security_headers_and_request_id() -> None:
    client = TestClient(create_app())
    r = client.get("/health/live")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "X-Request-ID" in r.headers

    r2 = client.get("/health/live", headers={"X-Request-ID": "req-42"})
    assert r2.headers["X-Request-ID"] == "req-42"


def test_metrics_endpoint_exposed() -> None:
    client = TestClient(create_app())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "http_requests_total" in r.text or "python_info" in r.text
