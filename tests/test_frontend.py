"""Frontend static mounts — landing at `/`, console at `/console`,
and proof that static mounts never shadow API routes."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "console" / "js"


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


def test_sse_reader_resumes_with_last_event_id() -> None:
    """Pin the console's replay contract: `sse.js` must track the `id:`
    line, echo it as `Last-Event-ID` on re-attach, and drop stale frames."""
    src = (_FRONTEND / "sse.js").read_text(encoding="utf-8")
    assert "Last-Event-ID" in src
    assert 'startsWith("id:")' in src
    assert "lastId" in src


def test_no_code_path_persists_token_to_localstorage() -> None:
    """Pin the sessionStorage-only decision (ADR 0006): nothing may read
    or write `orx_token` in localStorage — `removeItem` (legacy scrub) is
    the only permitted call — and no "remember me" affordance may exist."""
    js_dir = _FRONTEND
    sources = [js_dir / f for f in ("api.js", "auth.js", "sse.js")]
    sources += list(js_dir.glob("views/*.js"))
    sources.append(js_dir.parent / "index.html")  # console shell
    sources.append(js_dir.parent.parent / "index.html")  # landing
    for src_path in sources:
        src = src_path.read_text(encoding="utf-8")
        assert "localStorage.getItem" not in src, src_path
        assert "localStorage.setItem" not in src, src_path
    console_html = (js_dir.parent / "index.html").read_text(encoding="utf-8")
    assert "auth-remember" not in console_html


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


def test_budget_fields_document_zero_cap_semantics() -> None:
    """Both submission forms must tell the user what empty vs 0 means:
    empty = no cap, 0 = zero cap (pauses before any paid call)."""
    client = _client()
    landing = client.get("/").text
    console = client.get("/console/").text
    assert "Leave empty for no cap" in landing
    assert "0" in landing and "zero budget" in landing
    assert "Empty = no cap" in console
    assert "0 = zero budget" in console


def test_forms_omit_budget_only_when_empty() -> None:
    """The JS must omit ``budget_usd`` when the field is empty (no cap) but
    still send a literal ``0`` (zero cap)."""
    client = _client()
    landing = client.get("/").text
    jobs_js = client.get("/console/js/views/jobs.js").text
    assert "Number.isNaN(budget)" in landing
    assert "isNaN(parseFloat(f.budget_usd.value))" in jobs_js


def test_console_opens_hitl_modal_from_interrupt_payload() -> None:
    """jobs.js must consume ``allowed_actions`` from the interrupt SSE event —
    the payload path is what removed the re-escalation modal window."""
    js = _client().get("/console/js/views/jobs.js").text
    assert "ev.allowed_actions" in js
    assert "openHitl(ev.reason, ev.allowed_actions)" in js


def test_evidence_chips_deduplicated() -> None:
    """Duplicate manifest entries must not render duplicate evidence chips."""
    js = _client().get("/console/js/views/report.js").text
    assert "evidence_id" in js
    assert "seen.add" in js


def test_qa_artifacts_default_outside_repo(tmp_path) -> None:
    """A plain ``pytest`` run must not rewrite tracked files under ``docs/``:
    the QA output root is a temp dir unless ``QA_OUTPUT_DIR`` is set
    (the refresh script is the only path that repoints it at the repo)."""
    import os

    import pytest

    from tests.qa_output import REPO_ROOT, qa_output_dir

    if os.environ.get("QA_OUTPUT_DIR"):
        pytest.skip("explicit artifact-refresh run")
    assert not qa_output_dir().is_relative_to(REPO_ROOT)
