"""Browser E2E smoke test — real console UI against the offline backend.

Boots the app on an ephemeral port with the same doubles as the API suite
(StubLLM + fake search/scraper), drives the console in a headless browser,
and asserts the SSE transcript streams, the final report renders, and
source URLs become real links. This is the layer pytest can't otherwise
reach: EventSource lifecycle, the report renderer, and the modal plumbing.

Skipped automatically when Playwright or a browser binary is unavailable —
install with ``uv run --with playwright playwright install chromium``, or
rely on a local Chrome install (``channel="chrome"``).
"""

import socket
import threading
import time
import urllib.request
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm

pytestmark = pytest.mark.requires_browser

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment-dependent
    sync_playwright = None  # type: ignore[assignment]


@pytest.fixture()
def live_server(tmp_path: Any) -> Any:
    """Serve the stubbed app on an ephemeral port for a real browser."""
    settings = Settings(
        _env_file=None, scrape_delay_seconds=0.0, scrape_respect_robots=False
    )
    graph = build_graph(
        settings, llm=_stub_llm(verdict="approve"),
        search=_FakeSearch(), scraper=_FakeScraper(), checkpointer=None,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    db = str(tmp_path / "e2e.sqlite")
    runner = JobRunner(graph, JobStore(db), UsageStore(db), settings)
    app.state.runner = runner
    app.state.job_store = runner.job_store
    app.state.usage_store = runner.usage_store

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/ready", timeout=1
            )
            break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("live server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_console_full_flow(live_server: str) -> None:
    """Submit → live transcript → rendered report → real source links."""
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                pytest.skip(f"no browser binary available: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 860})
            page.goto(live_server + "/console/")
            page.fill('#job-form [name="topic"]', "SOC 2 vendor landscape")
            page.click("#submit-btn")

            # The job attaches and the SSE transcript starts streaming.
            page.wait_for_selector("#feed li", timeout=30000)
            # Stub graph converges: report card unhides when the job ends.
            page.wait_for_selector("#report-card:not(.hidden)", timeout=60000)

            # Sources render as real links — the stubbed citation URL must
            # be a clickable <a>, not bare text.
            page.wait_for_selector(
                '#report a[href="https://src.test/a"]', timeout=15000
            )
            # Audit seal verified + PDF action visible.
            page.wait_for_selector("#audit-badge:not(.hidden)", timeout=15000)
            assert "audit" in page.inner_text("#audit-badge")
            assert page.locator("#report-pdf").is_visible()
        finally:
            browser.close()
