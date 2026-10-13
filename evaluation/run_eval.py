"""Deterministic offline evaluation harness — the EDD quality gate.

Runs the dataset through the real compiled graph with a stubbed LLM and
fixture-based search/scrape (zero network, zero credentials). Exits non-zero
if any versioned threshold fails — the same gate CI enforces.

Usage:
    uv run python -m evaluation.run_eval              # evaluate + write scorecard
    uv run python -m evaluation.run_eval --report     # also refresh TREND.md
    uv run python -m evaluation.run_eval --langsmith  # + LangSmith experiment
    uv run python -m evaluation.run_eval --prompt-overrides writer=2,reviewer=1
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.prompt_eval import (
    EDD_THRESHOLDS,
    aggregate_metrics,
    brief_for_case,
    run_eval_case,
)
from app.services.prompt_registry import PromptRegistry, PromptRegistryError
from evaluation.dataset import DATASET, EvalCase

RESULTS_DIR = Path(__file__).parent / "results"
SCORECARD_PATH = RESULTS_DIR / "scorecard.json"
TREND_PATH = RESULTS_DIR / "TREND.md"

# Thresholds — identical to the project objective table.
THRESHOLDS = EDD_THRESHOLDS


async def _run_case(
    case: EvalCase, settings: Settings, registry: PromptRegistry
) -> dict[str, Any]:
    metrics, state = await run_eval_case(
        case_id=case.case_id,
        brief=brief_for_case(case),
        expect_blocked=case.expect_blocked,
        settings=settings,
        registry=registry,
    )
    metrics["prompt_versions"] = state.get("prompt_versions", {})
    return metrics


def _check_gates(metrics: dict[str, float]) -> dict[str, bool]:
    return {k: metrics[k] >= v for k, v in THRESHOLDS.items()}


def _write_trend(metrics: dict[str, float], results: list[dict[str, Any]]) -> None:
    lines = [
        "# EDD Trend History",
        "",
        "| Run (UTC) | citation_support | rubric_avg | convergence | blocked |",
        "|---|---|---|---|---|",
        (
            f"| {datetime.now(UTC).isoformat()} | "
            f"{metrics['citation_support_rate']:.3f} | "
            f"{metrics['rubric_score_avg']:.2f} | "
            f"{metrics['debate_convergence_rate']:.3f} | "
            f"{metrics['blocked_rate']:.3f} |"
        ),
        "",
        "## Latest case results",
        "",
    ]
    for r in results:
        lines.append(
            f"- `{r['case_id']}` — status={r['status']}, "
            f"support={r['citation_support']:.2f}, score={r['rubric_score']:.1f}, "
            f"rounds={r['debate_rounds']}"
        )
    TREND_PATH.write_text("\n".join(lines), encoding="utf-8")


def _parse_overrides(raw: str) -> dict[str, int]:
    """``writer=2,reviewer=1`` → {writer: 2, reviewer: 1}."""
    overrides: dict[str, int] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _eq, version = pair.partition("=")
        if not name or not version.isdigit():
            raise SystemExit(f"bad --prompt-overrides entry: {pair!r}")
        overrides[name.strip()] = int(version)
    return overrides


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="write TREND.md")
    parser.add_argument(
        "--langsmith",
        action="store_true",
        help="also publish the dataset and run a LangSmith experiment",
    )
    parser.add_argument(
        "--prompt-overrides",
        default="",
        help="pin prompt versions for the run, e.g. writer=2,reviewer=1",
    )
    args = parser.parse_args()

    settings = Settings(
        _env_file=None, scrape_delay_seconds=0.0, scrape_respect_robots=False
    )
    registry = PromptRegistry.load()  # YAML seeds only — deterministic eval
    overrides = _parse_overrides(args.prompt_overrides)
    if overrides:
        for name, version in overrides.items():
            try:
                registry.get(name, version)
            except PromptRegistryError as exc:
                print(f"invalid --prompt-overrides: {exc}", file=sys.stderr)
                return 2
        registry = registry.with_overrides(overrides)

    results = [await _run_case(c, settings, registry) for c in DATASET]
    metrics = aggregate_metrics(results)
    gates = _check_gates(metrics)

    prompt_versions: dict[str, int] = {}
    for r in results:
        prompt_versions.update(r.get("prompt_versions", {}))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scorecard = {
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "gates": gates,
        "prompt_versions": prompt_versions,
        "prompt_overrides": overrides,
        "cases": results,
        "mode": "offline (stub LLM + fixture tools)",
    }
    SCORECARD_PATH.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
    if args.report:
        _write_trend(metrics, results)

    if args.langsmith:
        from evaluation.langsmith_eval import run_langsmith_experiment

        name = await asyncio.to_thread(run_langsmith_experiment)
        if name:
            print(f"LangSmith experiment published: {name}")
        else:
            print("LangSmith skipped: LANGCHAIN_API_KEY not configured")

    print("Evaluation scorecard:")
    for k, v in metrics.items():
        mark = "PASS" if gates[k] else "FAIL"
        print(f"  {mark} {k}: {v:.3f} (threshold {THRESHOLDS[k]})")
    failed = [k for k, ok in gates.items() if not ok]
    if failed:
        print(f"FAILED gates: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("All EDD gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
