"""Export operator feedback as candidate eval cases.

Reads the feedback table from the app database and writes one JSON file
per row into ``evaluation/feedback-queue/``. These are *candidate* cases
only — a human reviews each file and manually promotes worthy entries
into the gated EDD dataset; nothing here is ever auto-added.

Usage::

    uv run python scripts/export_feedback.py [--db data/orchestrator.sqlite]
                                           [--workspace default]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.feedback_store import FeedbackStore, dumps_case  # noqa: E402

OUT_DIR = ROOT / "evaluation" / "feedback-queue"


async def _run(db: str, workspace: str) -> int:
    store = FeedbackStore(db)
    cases = await store.export_cases(workspace)
    if not cases:
        print("no feedback rows — nothing to export")
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for case in cases:
        path = OUT_DIR / f"{case['candidate_case']['id']}-{case['job_id'][:8]}.json"
        path.write_text(dumps_case(case), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return len(cases)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/orchestrator.sqlite")
    ap.add_argument("--workspace", default="default")
    args = ap.parse_args()
    n = asyncio.run(_run(args.db, args.workspace))
    print(f"exported {n} candidate case(s) to {OUT_DIR.relative_to(ROOT)}/")
    print("next: review each file, then promote worthy cases manually.")


if __name__ == "__main__":
    main()
