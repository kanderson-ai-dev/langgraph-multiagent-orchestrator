"""Export a registry prompt version as a reviewable ``vN.yaml`` seed file.

Runtime candidates (created via the API/console) live only in the SQLite
overlay. This command writes one version to ``prompts/<name>/v<version>.yaml``
in exactly the seed shape, so a reviewed prompt can be committed and
adopted as a YAML seed at the next startup.

Refuses to overwrite an existing file — a committed seed is never
rewritten by a script. Prints to stdout when ``--write`` is not given.

Usage::

    uv run python scripts/export_prompt.py writer 2
    uv run python scripts/export_prompt.py writer 2 --write
    uv run python scripts/export_prompt.py writer 2 --write --db data/orchestrator.sqlite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.prompt_registry import (  # noqa: E402
    DEFAULT_PROMPTS_DIR,
    PromptRegistry,
    PromptRegistryError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="prompt name (e.g. writer)")
    parser.add_argument("version", type=int, help="version number")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write prompts/<name>/v<version>.yaml (never overwrites)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="registry DB path (default: settings.database_path)",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_path = args.db or settings.database_path
    registry = PromptRegistry.load(db_path=db_path)
    try:
        body = registry.export_yaml(args.name, args.version)
    except PromptRegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.write:
        print(body, end="")
        return 0

    target = DEFAULT_PROMPTS_DIR / args.name / f"v{args.version}.yaml"
    if target.exists():
        print(f"error: {target} already exists — refusing to overwrite",
              file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
