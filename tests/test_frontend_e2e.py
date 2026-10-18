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
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.graph.graph import build_graph
from app.main import create_app
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.usage_store import UsageStore
from tests.qa_output import qa_output_dir
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm

pytestmark = pytest.mark.requires_browser

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment-dependent
    sync_playwright = None  # type: ignore[assignment]


@pytest.fixture()
def live_server(tmp_path: Any) -> Any:
    """Serve the stubbed app on an ephemeral port for a real browser."""
    from app.api.v1.routes.prompts import get_prompt_evaluator
    from app.services.prompt_eval import ABResult
    from app.services.prompt_registry import PromptRegistry

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
    app.state.prompt_registry = PromptRegistry.load(db_path=":memory:")

    # Deterministic A/B evaluator: the candidate always regresses vs the
    # promoted version — the metric gate must block promote until an
    # override reason is supplied (mirrors the unit-suite fixture).
    clean = {"citation_support_rate": 1.0, "rubric_score_avg": 4.5,
             "debate_convergence_rate": 1.0, "blocked_rate": 1.0}
    regressed = {**clean, "rubric_score_avg": 3.0,
                 "debate_convergence_rate": 0.5}

    async def evaluator(name: str, version: int) -> ABResult:
        return ABResult(
            name=name,
            promoted_version=max(version - 1, 1),
            candidate_version=version,
            promoted_metrics=clean,
            candidate_metrics=regressed,
            deltas={k: round(regressed[k] - clean[k], 6) for k in clean},
        )

    app.dependency_overrides[get_prompt_evaluator] = lambda: evaluator

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
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", db=db, app=app)
    server.should_exit = True
    thread.join(timeout=10)


def _browser(p: Any) -> Any:
    """Chromium via local Chrome first, bundled binary as fallback."""
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return p.chromium.launch(headless=True)


def test_console_full_flow(live_server: Any) -> None:
    """Submit → live transcript → rendered report → real source links."""
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        try:
            browser = _browser(p)
        except Exception as exc:
            pytest.skip(f"no browser binary available: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 860}, bypass_csp=True)
            page.goto(live_server.url + "/console/")
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
            # Prompt-version chips + sealed evidence hashes render in the
            # report card (stub graph produces both).
            page.wait_for_selector("#prompt-versions .chip-prompt",
                                   timeout=15000)
            page.wait_for_selector("#evidence-strip .chip-evidence",
                                   timeout=15000)
        finally:
            browser.close()


def test_prompt_registry_tab_flow(live_server: Any) -> None:
    """Candidate → gated promote blocked → override → rollback, all in UI."""
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        try:
            browser = _browser(p)
        except Exception as exc:
            pytest.skip(f"no browser binary available: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900}, bypass_csp=True)
            page.goto(live_server.url + "/console/")
            page.click('[data-tab="prompts"]')

            # Registry lists seeds; select writer → template prefilled.
            page.wait_for_selector('.prompt-name[data-name="writer"]',
                                   timeout=15000)
            page.click('.prompt-name[data-name="writer"]')
            page.wait_for_selector("#versions-body tr", timeout=15000)
            assert page.input_value("#candidate-template") != ""

            # Register a candidate (writer declares no variables — a
            # template without placeholders keeps the contract).
            page.fill("#candidate-template",
                      "Writer v2 — draft even more carefully.")
            page.fill("#candidate-changelog", "e2e candidate")
            page.click("#candidate-submit")
            page.wait_for_function(
                "document.querySelectorAll('#versions-body tr').length > 1",
                timeout=15000)

            # Promote v2 → metric gate blocks (regressing evaluator).
            page.fill("#promote-version", "2")
            page.click("#promote-run")
            page.wait_for_selector("#promote-result .modal-error",
                                   timeout=30000)
            page.wait_for_selector("#override-box:not(.hidden)",
                                   timeout=15000)

            # Override with a real reason → promote succeeds.
            page.fill("#override-reason",
                      "e2e: measured regression accepted for QA demo")
            page.click("#promote-override-run")
            page.wait_for_selector("#promote-result .ok-text",
                                   timeout=30000)

            # Roll back to v1.
            page.fill("#rollback-version", "1")
            page.fill("#rollback-reason", "e2e rollback")
            page.click("#rollback-run")
            page.wait_for_selector("#promote-result .ok-text",
                                   timeout=30000)
        finally:
            browser.close()


def test_performance_and_audit_tabs(live_server: Any) -> None:
    """Performance empty→populated states; audit verify + tamper flag."""
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    # Seed one completed job + usage row so the populated state is real.
    import asyncio
    import threading

    async def seed() -> str:
        from app.graph.state import AgentMessage, Brief
        from app.services.audit import audit_root, chain_transcript

        jobs = live_server.app.state.job_store
        usage = live_server.app.state.usage_store
        brief = Brief.model_validate(
            {"topic": "seeded report", "report_type": "general",
             "budget_usd": 1.0}
        )
        job = await jobs.create("default", brief)
        transcript = [
            AgentMessage(sender="writer", recipient="reviewer",
                         kind="feedback", content="draft v1"),
            AgentMessage(sender="reviewer", recipient="supervisor",
                         kind="verdict", content="approve"),
        ]
        await jobs.set_result(
            "default", job.job_id,
            {
                "report": "done",
                "debate_rounds": 1,
                "transcript": [m.model_dump(mode="json") for m in transcript],
                "audit_root": audit_root(transcript),
                "audit_entries": [
                    e.model_dump() for e in chain_transcript(transcript)
                ],
            },
            cost_usd=0.002, status="done",
        )
        await usage.record(
            job_id=job.job_id, workspace_id="default", role="writer",
            model="gpt-4o-mini", prompt_tokens=10, completion_tokens=5,
            cost_usd=0.002, prompt_name="writer", prompt_version=1,
        )
        return job.job_id

    def run_seed() -> str:
        # playwright's sync API owns the main thread's loop — run the
        # async seed in a worker thread with its own loop.
        box: list[str] = []

        def _go() -> None:
            box.append(asyncio.run(seed()))

        t = threading.Thread(target=_go)
        t.start()
        t.join()
        return box[0]

    with sync_playwright() as p:
        try:
            browser = _browser(p)
        except Exception as exc:
            pytest.skip(f"no browser binary available: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900}, bypass_csp=True)
            page.goto(live_server.url + "/console/")

            # Performance tab — empty state first (no completed jobs yet).
            page.click('[data-tab="performance"]')
            page.wait_for_selector("#perf-body .empty-note", timeout=15000)
            assert "no completed jobs" in page.inner_text("#perf-body").lower()

            job_id = run_seed()
            page.reload()
            page.click('[data-tab="performance"]')
            page.wait_for_selector("#perf-body .perf-section", timeout=15000)
            # inner_text applies text-transform — labels render uppercase.
            assert "SUCCESS RATE" in page.inner_text("#perf-body")
            assert "HUMAN VS AGENT" in page.inner_text("#perf-body")

            # Audit tab — select the seeded job and verify.
            page.click('[data-tab="audit"]')
            page.select_option("#audit-job-select", job_id)
            page.click("#audit-verify")
            page.wait_for_selector("#audit-verify-badge.chip-ok",
                                   timeout=15000)
            assert "valid" in page.inner_text("#audit-verify-badge")

            # Tamper the stored transcript → re-verify flips to INVALID.
            # (sync sqlite3 — playwright's sync API owns the event loop)
            import sqlite3
            with sqlite3.connect(live_server.db) as db:
                db.execute(
                    "UPDATE jobs SET result_json ="
                    " json_replace(result_json,"
                    " '$.transcript[0].content', 'tampered')"
                    " WHERE job_id = ?",
                    (job_id,),
                )
                db.commit()
            page.click("#audit-verify")
            page.wait_for_selector("#audit-verify-badge.chip-bad",
                                   timeout=15000)
            assert "INVALID" in page.inner_text("#audit-verify-badge")
        finally:
            browser.close()


def test_console_responsive(live_server: Any) -> None:
    """375/768/1280 px: no horizontal overflow, all four tabs reachable."""
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    shots = qa_output_dir() / "docs" / "ui-qa" / "breakpoints"
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            browser = _browser(p)
        except Exception as exc:
            pytest.skip(f"no browser binary available: {exc}")
        try:
            for width in (375, 768, 1280):
                page = browser.new_page(
                    viewport={"width": width, "height": 800}, bypass_csp=True
                )
                page.goto(live_server.url + "/console/")
                for tab in ("jobs", "prompts", "performance", "audit"):
                    page.click(f'[data-tab="{tab}"]')
                    page.wait_for_function(
                        "document.documentElement.scrollWidth"
                        " <= document.documentElement.clientWidth + 1",
                        timeout=10000)
                    page.screenshot(
                        path=str(shots / f"console-{tab}-{width}px.png"),
                        full_page=True,
                    )
                page.close()
        finally:
            browser.close()
