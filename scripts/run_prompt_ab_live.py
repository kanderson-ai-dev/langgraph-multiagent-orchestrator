"""Run a live Langfuse experiment with pinned prompt versions (A/B).

The registry's ``with_overrides`` view resolves ``get``/``render`` to the
pinned versions without promoting them, so a candidate can be evaluated
head-to-head against the promoted set on the same golden dataset. The
experiment run name picks up the pinned writer version
(``writer-v<N>-<git sha>-<date>``), which makes Langfuse's compare view a
true candidate-vs-promoted A/B.

Real OpenAI calls + real search/scrape — budget ~$0.02-0.05 per run.
Manual trigger only; never in CI.

Usage (project root):

    uv run python scripts/run_prompt_ab_live.py --pin writer=2
    uv run python scripts/run_prompt_ab_live.py --pin writer=2 --pin reviewer=3
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.services.prompt_registry import PromptRegistry  # noqa: E402
from evaluation.langfuse_eval import run_langfuse_experiment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="NAME=VERSION",
        help="pin a prompt to a specific version (repeatable)",
    )
    args = parser.parse_args()
    pins: dict[str, int] = {}
    for spec in args.pin:
        name, sep, version = spec.partition("=")
        if not sep or not name:
            parser.error(f"bad --pin {spec!r}; expected NAME=VERSION")
        pins[name] = int(version)

    # The app database overlay — runtime candidates live there, not in
    # the YAML seeds, so a --pin to a candidate needs the same store the
    # running app uses.
    registry = PromptRegistry.load(db_path=Settings().database_path)
    if pins:
        registry = registry.with_overrides(pins)
        pinned = ", ".join(f"{n}@v{v}" for n, v in pins.items())
        print(f"A/B run with pinned prompts: {pinned}")

    run_name = run_langfuse_experiment(live=True, registry=registry)
    if run_name is None:
        print("Langfuse skipped: keys not configured", file=sys.stderr)
        return 1
    print(f"Langfuse experiment published: {run_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
