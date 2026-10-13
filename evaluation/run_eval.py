"""Deterministic offline evaluation harness — the EDD quality gate.

Runs the dataset through the real compiled graph with a stubbed LLM and
fixture-based search/scrape (zero network, zero credentials). Exits non-zero
if any versioned threshold fails — the same gate CI enforces.

Each case also runs the single-prompt baseline (one LLM call, no
orchestration, no verification) over the *same* evidence bundle, and the
scorecard stores both columns side-by-side — the honest counterfactual the
pipeline must justify. Baseline numbers are informational, never gated.

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
from evaluation.baseline import aggregate_baseline, run_baseline_case
from evaluation.dataset import DATASET, EvalCase
from evaluation.trend import (
    append_history,
    build_history_row,
    read_history,
    render_trend,
)

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
    metrics["baseline"] = await run_baseline_case(
        case_id=case.case_id,
        brief=brief_for_case(case),
        evidence=state.get("evidence", []),
        settings=settings,
        review_system=registry.render("reviewer").text,
    )
    return metrics


def _check_gates(metrics: dict[str, float]) -> dict[str, bool]:
    return {k: metrics[k] >= v for k, v in THRESHOLDS.items()}


def build_scorecard(
    metrics: dict[str, float],
    baseline: dict[str, float],
    gates: dict[str, bool],
    results: list[dict[str, Any]],
    prompt_versions: dict[str, int],
    overrides: dict[str, int],
    settings: Settings,
    mode: str,
) -> dict[str, Any]:
    """The versioned scorecard payload — pipeline and baseline side-by-side."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": settings.chat_model_name,
        "judge_model": settings.judge_model,
        "metrics": metrics,
        "baseline_metrics": baseline,
        "thresholds": THRESHOLDS,
        "gates": gates,
        "prompt_versions": prompt_versions,
        "prompt_overrides": overrides,
        "cases": results,
        "mode": mode,
    }


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


def _print_scorecard(
    metrics: dict[str, float],
    baseline: dict[str, float],
    gates: dict[str, bool],
) -> None:
    print("Evaluation scorecard (pipeline vs single-prompt baseline):")
    informational = {
        "avg_cost_usd": ("avg_cost_usd", ".6f"),
        "avg_latency_seconds": ("avg_latency_seconds", ".2f"),
        "avg_llm_calls": ("avg_llm_calls", ".1f"),
    }
    for metric, threshold in THRESHOLDS.items():
        mark = "PASS" if gates[metric] else "FAIL"
        # Metrics the baseline has no notion of (e.g. debate convergence)
        # render as n/a rather than a fake 0.000.
        base = (
            f"{baseline[metric]:.3f}" if metric in baseline else "n/a"
        )
        print(
            f"  {mark} {metric}: pipeline={metrics[metric]:.3f} "
            f"baseline={base} (threshold {threshold})"
        )
    for key, (base_key, fmt) in informational.items():
        print(
            f"  --- {key}: pipeline={metrics[key]:{fmt}} "
            f"baseline={baseline.get(base_key, 0.0):{fmt}} (informational)"
        )


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="write TREND.md")
    parser.add_argument(
        "--langsmith",
        action="store_true",
        help="also publish the dataset and run a LangSmith experiment",
    )
    parser.add_argument(
        "--langsmith-live",
        action="store_true",
        help="publish a real-LLM LangSmith experiment (requires OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--langfuse-live",
        action="store_true",
        help="publish a real-LLM Langfuse experiment "
        "(requires LANGFUSE_* keys + OPENAI_API_KEY)",
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

    mode = "offline (stub LLM + fixture tools)"
    results = [await _run_case(c, settings, registry) for c in DATASET]
    metrics = aggregate_metrics(results)
    baseline = aggregate_baseline(results)
    gates = _check_gates(metrics)

    prompt_versions: dict[str, int] = {}
    for r in results:
        prompt_versions.update(r.get("prompt_versions", {}))
    # The judge is a versioned registry prompt too — record what scored.
    prompt_versions["judge"] = registry.render("judge").version

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scorecard = build_scorecard(
        metrics, baseline, gates, results, prompt_versions, overrides,
        settings, mode,
    )
    SCORECARD_PATH.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")

    # Append-only trend history — every run is preserved.
    append_history(
        build_history_row(
            metrics=metrics, baseline=baseline, results=results,
            prompt_versions=prompt_versions, settings=settings, mode=mode,
        )
    )
    if args.report:
        render_trend(read_history(), latest_results=results)

    if args.langsmith or args.langsmith_live:
        from evaluation.langsmith_eval import run_langsmith_experiment

        name = await asyncio.to_thread(
            run_langsmith_experiment, live=args.langsmith_live
        )
        if name:
            print(f"LangSmith experiment published: {name}")
        else:
            print("LangSmith skipped: required key not configured")

    if args.langfuse_live:
        from evaluation.langfuse_eval import run_langfuse_experiment

        name = await asyncio.to_thread(run_langfuse_experiment, live=True)
        if name:
            print(f"Langfuse experiment published: {name}")
        else:
            print("Langfuse skipped: required keys not configured")

    _print_scorecard(metrics, baseline, gates)
    failed = [k for k, ok in gates.items() if not ok]
    if failed:
        print(f"FAILED gates: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("All EDD gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
