"""Record the end-to-end demo as an animated GIF for the README.

Drives real headless Chrome through the whole flow: landing brief → live
console transcript → HITL escalation (a small budget cap forces
``budget_exceeded``) → "Fund & continue" → sealed final report. Frames are
captured continuously and assembled into ``docs/screenshots/demo.gif`` with
per-phase durations: normal speed on the landing, fast-forwarded transcript,
longer dwell on the HITL modal and the final report.

Usage (project root, API running on :8000):

    uv run --with playwright python scripts/record_demo.py

Uses the locally installed Chrome channel — no browser download required.
"""

import io
import json
import sys
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = "docs/screenshots/demo.gif"

# Per-phase frame dwell (ms) — transcript fast, key moments slower.
DUR = {"landing": 900, "run": 130, "hitl": 1800, "report": 1400, "final": 2800}

BRIEF = {
    "topic": "Evaluate Vanta vs Drata as SOC 2 compliance vendors",
    "audience": "CISO and procurement lead",
    "tone": "executive",
    "report_type": "vendor_assessment",
    "budget": "0.006",  # small cap → guaranteed budget_exceeded escalation
    "context": "Series B SaaS company, 80 employees, needs SOC 2 Type II within 6 months",
    "requirements": "Pricing comparison\nSecurity and compliance posture\nRecommendation",
}

frames: list[tuple[bytes, int]] = []  # (png bytes, duration ms)


def shot(page, phase: str, count: int = 1) -> None:
    for _ in range(count):
        frames.append((page.screenshot(), DUR[phase]))


def poll_job(job_id: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/api/v1/orchestration/reports/{job_id}") as r:
        return json.loads(r.read())


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 860})

        # ── Landing: normal-speed form fill ────────────────────────────
        page.goto(BASE + "/", wait_until="networkidle")
        shot(page, "landing")
        page.click("#topic")
        page.locator("#topic").press_sequentially(BRIEF["topic"], delay=18)
        shot(page, "landing")
        page.fill("#audience", BRIEF["audience"])
        page.select_option("#tone", BRIEF["tone"])
        page.select_option("#report_type", BRIEF["report_type"])
        page.fill("#budget", BRIEF["budget"])
        page.fill("#tenant_context", BRIEF["context"])
        page.fill("#requirements", BRIEF["requirements"])
        shot(page, "landing", 2)
        page.click("#submit-btn")

        # ── Console: fast-forwarded transcript ─────────────────────────
        try:
            page.wait_for_function(
                "location.pathname === '/console/'", timeout=20000
            )
        except Exception:
            err = page.locator("#form-error")
            detail = err.inner_text() if err.is_visible() else page.url
            raise RuntimeError(
                f"submit did not reach console: {detail}"
            ) from None
        job_id = page.url.split("job=")[1]
        deadline = time.time() + 480
        status = "running"
        while time.time() < deadline:
            job = poll_job(job_id)
            status = job["status"]
            if status in ("done", "failed", "blocked"):
                break
            if status == "awaiting_review":
                # The modal opens via SSE on each escalation; the fund button
                # is only rendered for budget escalations — that's the
                # DOM-truthful signal for which action to take.
                if page.locator("#hitl-modal").is_visible():
                    shot(page, "hitl", 2)
                    if page.locator('[data-action="fund"]').is_visible():
                        page.fill("#hitl-fund", "0.05")
                        shot(page, "hitl")
                        page.click('[data-action="fund"]')
                    else:
                        page.click('[data-action="approve"]')
                else:
                    shot(page, "run")
                    time.sleep(0.5)
                continue
            shot(page, "run")
            time.sleep(0.55)

        # ── Final report: dwell on the sealed deliverable ──────────────
        if status == "done":
            try:
                page.wait_for_selector(
                    "#report-card:not(.hidden)", timeout=15000
                )
            except Exception:
                page.reload()
                page.wait_for_selector(
                    "#report-card:not(.hidden)", timeout=15000
                )
            shot(page, "report", 2)
            # Scroll the report card down through its sections to the
            # Sources block at the bottom.
            for _ in range(14):
                at_bottom = page.eval_on_selector(
                    "#report-card",
                    "el => { el.scrollTop += 420;"
                    "        return el.scrollTop + el.clientHeight"
                    "               >= el.scrollHeight - 4; }",
                )
                shot(page, "report")
                if at_bottom:
                    break
            # Scroll the page so the workspace cost panel (below Recent
            # jobs) comes into view; the pinned cards stay visible.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
            shot(page, "report")
            shot(page, "final")
        else:
            print(f"demo job ended as {status}", file=sys.stderr)

        browser.close()

    # ── Assemble GIF (dedupe consecutive identical frames) ─────────────
    out_frames: list[Image.Image] = []
    durations: list[int] = []
    prev_png: bytes | None = None
    for png, dur in frames:
        if out_frames and png == prev_png:
            durations[-1] += dur
            continue
        prev_png = png
        img = Image.open(io.BytesIO(png)).convert("RGB")
        w = 1100
        img = img.resize((w, int(img.height * w / img.width)), Image.LANCZOS)
        out_frames.append(img)
        durations.append(dur)

    out_frames[0].save(
        OUT,
        save_all=True,
        append_images=[f.convert("P", palette=Image.ADAPTIVE) for f in out_frames[1:]],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(f"saved {OUT} — {len(out_frames)} frames")


if __name__ == "__main__":
    main()
