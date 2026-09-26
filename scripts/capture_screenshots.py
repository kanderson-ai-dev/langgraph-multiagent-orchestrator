"""Capture the README stills that complement the demo GIF.

The end-to-end flow (landing, live transcript, HITL modal, report, workspace
costs) is already covered by ``docs/screenshots/demo.gif`` — this script only
captures the section stills the GIF doesn't show well: the sealed report card
with the audit badge, the OpenAPI surface, and the EDD scorecard.

Usage (server must already be running — e.g. ``uv run uvicorn app.main:app``;
a completed job must exist in Recent jobs for the report shot):

    uv run --with playwright python scripts/capture_screenshots.py [BASE_URL]

Outputs land in ``docs/screenshots/`` under the names the README embeds.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed — run with `uv run --with playwright`")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def _browser(p):
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return p.chromium.launch(headless=True)


def terminal_screenshot(page, text: str, name: str) -> None:
    """Render captured stdout into a fake-terminal page and screenshot it."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<html><body style="margin:0;background:#0b0f14;padding:24px">
<pre style="color:#c9d1d9;font:14px/1.5 ui-monospace,Consolas,monospace;
white-space:pre-wrap;margin:0">{escaped}</pre></body></html>"""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        tmp = f.name
    page.goto(f"file:///{tmp.replace(chr(92), '/')}")
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / name))
    print(f"captured {name}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        page = _browser(p).new_page(viewport={"width": 1440, "height": 900})

        # 1. Sealed report card — open the newest completed job
        page.goto(f"{BASE}/console/", wait_until="networkidle")
        done_job = page.locator("#jobs-list .jobs-item:has(.status.done)").first
        done_job.wait_for(timeout=15000)
        done_job.click()
        page.wait_for_selector("#report-card:not(.hidden)", timeout=30000)
        page.locator("#report-card").scroll_into_view_if_needed()
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "report-audit-badge.png"))
        print("captured report-audit-badge.png")

        # 2. Swagger UI — the auto-generated API surface
        page.goto(f"{BASE}/docs", wait_until="networkidle")
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "swagger-docs.png"))
        print("captured swagger-docs.png")

        page.context.browser.close()

    # 3. EDD scorecard — real eval output rendered as a terminal shot
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "-m", "evaluation.run_eval"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        # stdout only — uv warns about VIRTUAL_ENV on stderr, leaking local paths
        text = proc.stdout.strip() or "run_eval produced no output"
    except Exception as exc:
        text = f"run_eval failed: {exc}"
    with sync_playwright() as p:
        page = _browser(p).new_page(viewport={"width": 1100, "height": 500})
        terminal_screenshot(page, text, "edd-scorecard.png")
        page.context.browser.close()


if __name__ == "__main__":
    main()
