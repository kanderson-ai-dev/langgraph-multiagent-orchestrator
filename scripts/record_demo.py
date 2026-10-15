"""Record the end-to-end demo as an animated GIF for the README.

Drives real headless Chrome through the whole flow at *readable* pace:
landing page scroll → brief fill → console live transcript (Supervisor
dispatches, Writer↔Reviewer debate) → HITL escalation (a small budget cap
forces ``budget_exceeded``) → "Fund & continue" → sealed final report →
tour of the Prompt Registry, Performance & Cost and Audit tabs. Frames are
captured continuously and assembled into ``docs/screenshots/demo.gif``;
consecutive identical frames collapse into one longer dwell, so calm
moments read slowly without inflating the file.

Usage (project root, API running on :8000):

    uv run --with playwright python scripts/record_demo.py

Uses the locally installed Chrome channel — no browser download required.
"""

import contextlib
import io
import json
import sys
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = "docs/screenshots/demo.gif"

# Per-phase frame dwell (ms) — slow enough that a viewer can actually read
# each surface; identical frames dedupe, so static content costs one frame.
DUR = {
    "landing": 2600,
    "run": 700,
    "hitl": 3600,
    "report": 2200,
    "tab": 4200,
    "final": 4500,
}

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


def slow_scroll(page, selector: str, steps: int = 10, delta: int = 320) -> None:
    """Scroll a page or card in visible steps, one frame per step."""
    for _ in range(steps):
        at_bottom = page.eval_on_selector(
            selector,
            f"el => {{ el.scrollTop += {delta};"
            "        return el.scrollTop + el.clientHeight"
            "               >= el.scrollHeight - 4; }",
        )
        shot(page, "landing")
        if at_bottom:
            break


def pick_hitl_action(page) -> None:
    """Click the first *visible* guided action — the server's
    ``allowed_actions`` decides which buttons are real for this pause."""
    try:
        page.wait_for_selector(
            "#hitl-modal [data-action]:not([disabled]):not(.hidden)",
            timeout=10000,
        )
    except Exception:
        return
    if page.locator('[data-action="approve"]').is_visible():
        page.click('[data-action="approve"]')
    elif page.locator('[data-action="fund"]').is_visible():
        page.locator("#hitl-fund").press_sequentially("0.05", delay=80)
        shot(page, "hitl")
        page.click('[data-action="fund"]')
    elif page.locator('[data-action="edit"]').is_visible():
        page.locator("#hitl-feedback").press_sequentially(
            "Tighten the executive summary and re-cite.", delay=25
        )
        shot(page, "hitl")
        page.click('[data-action="edit"]')
    else:
        page.fill("#hitl-feedback", "Rejected for the demo.")
        page.click('[data-action="reject"]')


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 860})

        # ── Landing: read the page, then fill the brief slowly ────────
        page.goto(BASE + "/", wait_until="networkidle")
        shot(page, "landing", 2)
        # Scroll the landing so the viewer reads it top to bottom.
        for _ in range(10):
            at_bottom = page.evaluate(
                "window.scrollBy(0, 340);"
                " window.scrollY + innerHeight >= document.body.scrollHeight - 4"
            )
            shot(page, "landing")
            if at_bottom:
                break
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        shot(page, "landing")
        page.click("#topic")
        page.locator("#topic").press_sequentially(BRIEF["topic"], delay=45)
        shot(page, "landing")
        page.fill("#audience", BRIEF["audience"])
        page.select_option("#tone", BRIEF["tone"])
        page.select_option("#report_type", BRIEF["report_type"])
        page.fill("#budget", BRIEF["budget"])
        page.fill("#tenant_context", BRIEF["context"])
        page.fill("#requirements", BRIEF["requirements"])
        shot(page, "landing", 3)
        page.click("#submit-btn")

        # ── Console: transcript at readable speed ─────────────────────
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
                if page.locator("#hitl-modal").is_visible():
                    shot(page, "hitl", 3)
                    pick_hitl_action(page)
                else:
                    shot(page, "run")
                    time.sleep(0.6)
                continue
            shot(page, "run")
            time.sleep(0.7)

        # ── Final report: dwell, then scroll through it slowly ────────
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
            shot(page, "report", 3)
            for _ in range(16):
                at_bottom = page.eval_on_selector(
                    "#report-card",
                    "el => { el.scrollTop += 340;"
                    "        return el.scrollTop + el.clientHeight"
                    "               >= el.scrollHeight - 4; }",
                )
                shot(page, "report")
                if at_bottom:
                    break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
            shot(page, "report")

            # ── Report feedback widget: thumbs-up → queued ────────────
            try:
                up = page.locator('.fb-btn[data-rating="up"]')
                if up.is_visible():
                    up.click()
                    page.wait_for_function(
                        "document.querySelector('#fb-status')"
                        ".textContent.trim() !== ''",
                        timeout=8000,
                    )
                    shot(page, "report", 2)
            except Exception:
                pass

            # ── Tour the other console surfaces ───────────────────────
            _tour_prompt_registry(page)
            _tour_performance(page)
            _tour_audit(page, job_id)

            # ── Public surfaces: status endpoint + Swagger API docs ───
            page.goto(BASE + "/status", wait_until="networkidle")
            shot(page, "tab", 2)
            page.goto(BASE + "/docs", wait_until="networkidle")
            shot(page, "tab", 2)

            # ── Close on the deep link: reload restores the finished
            # job — report, transcript, seal — from ?job=<id> alone. ───
            page.goto(BASE + "/console/?job=" + job_id,
                      wait_until="networkidle")
            with contextlib.suppress(Exception):
                page.wait_for_selector(
                    "#report-card:not(.hidden)", timeout=15000
                )
            shot(page, "final", 2)
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


def _tour_prompt_registry(page) -> None:
    """Prompt Registry tab — names, versions, and a real diff."""
    page.click("#tabbtn-prompts")
    try:
        page.wait_for_selector("#prompt-names [data-name]", timeout=8000)
    except Exception:
        shot(page, "tab")
        return
    shot(page, "tab")
    # Select the writer prompt and show its versions table.
    writer = page.locator('#prompt-names [data-name="writer"]')
    if writer.count():
        writer.first.click()
        page.wait_for_timeout(600)
    shot(page, "tab", 2)
    # A real v1→v2 diff when two versions exist.
    if page.locator("#versions-body tr").count() >= 2:
        page.fill("#diff-from", "1")
        page.fill("#diff-to", "2")
        page.click("#diff-run")
        page.wait_for_timeout(800)
        shot(page, "tab", 2)
    # Scroll the registry column so candidate/promote cards are readable.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(300)
    shot(page, "tab")


def _tour_performance(page) -> None:
    """Performance & Cost tab — p50/p95, cost per job, human-vs-agent."""
    page.click("#tabbtn-performance")
    page.evaluate("window.scrollTo(0, 0)")
    with contextlib.suppress(Exception):
        page.wait_for_function(
            "document.querySelector('#perf-body').children.length > 0",
            timeout=8000,
        )
    shot(page, "tab", 2)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(300)
    shot(page, "tab")


def _tour_audit(page, job_id: str) -> None:
    """Audit tab — pick the demo job and verify its hash chain live."""
    page.click("#tabbtn-audit")
    page.evaluate("window.scrollTo(0, 0)")
    try:
        # <option> elements inside <select> never count as "visible" —
        # attach-wait on a non-empty value only (the placeholder has
        # value="" and would satisfy a bare [value] selector).
        page.wait_for_selector(
            "#audit-job-select option[value]:not([value=''])",
            state="attached", timeout=8000,
        )
    except Exception:
        shot(page, "tab")
        return
    options = page.locator("#audit-job-select option")
    values = [
        options.nth(i).get_attribute("value") for i in range(options.count())
    ]
    target = job_id if job_id in values else next((v for v in values if v), None)
    if not target:
        shot(page, "tab")
        return
    page.select_option("#audit-job-select", target)
    page.click("#audit-verify")
    with contextlib.suppress(Exception):
        page.wait_for_function(
            "document.querySelector('#audit-verify-badge').textContent.trim() !== ''",
            timeout=8000,
        )
    shot(page, "tab", 2)
    # Reveal the sealed evidence manifest below the verification stats.
    page.evaluate("window.scrollTo(0, 260)")
    page.wait_for_timeout(300)
    shot(page, "tab")


if __name__ == "__main__":
    main()
