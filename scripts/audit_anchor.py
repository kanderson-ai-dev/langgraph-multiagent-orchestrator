"""Print or verify the append-only audit anchor log.

Every sealed job appends one JSONL line binding ``job_id`` → ``audit_root``
independent of the jobs database. This helper is the owner-facing surface:

- ``latest`` prints the newest anchor — publish this root off-box (a Git
  tag, a transparency log, a notarized gist) and later verification proves
  the DB was not rewritten since.
- ``--job <id>`` prints that job's anchor entry.
- ``--check`` compares every anchor against the jobs DB and exits ``1``
  when any stored root was rewritten.

Usage::

    uv run python scripts/audit_anchor.py latest
    uv run python scripts/audit_anchor.py latest --job 42
    uv run python scripts/audit_anchor.py --check --db data/orchestrator.sqlite
    uv run python scripts/audit_anchor.py latest --anchor data/audit_anchor.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import audit_anchor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "action",
        nargs="?",
        default="latest",
        choices=["latest"],
        help="what to print (default: latest anchor)",
    )
    parser.add_argument("--job", dest="job_id", default=None, help="job id")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare every anchor with the jobs DB instead",
    )
    parser.add_argument(
        "--anchor",
        default=None,
        help="anchor log path (default: AUDIT_ANCHOR_PATH)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="jobs DB path for --check (default: DATABASE_PATH)",
    )
    args = parser.parse_args()

    settings = get_settings()
    anchor_path = Path(args.anchor or settings.audit_anchor_path)
    db_path = Path(args.db or settings.database_path)

    if args.check:
        rows = audit_anchor.check_db(anchor_path, db_path)
        drift = [r for r in rows if r["match"] is False]
        print(
            json.dumps(
                {
                    "anchors": len(rows),
                    "checked": sum(r["match"] is not None for r in rows),
                    "drifted": [r["job_id"] for r in drift],
                },
                indent=2,
            )
        )
        return 1 if drift else 0

    entry = audit_anchor.latest(anchor_path, job_id=args.job_id)
    if entry is None:
        print("no anchor entries", file=sys.stderr)
        return 1
    print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
