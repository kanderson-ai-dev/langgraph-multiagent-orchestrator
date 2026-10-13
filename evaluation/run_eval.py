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
) -> dict[str, Any]:
    """The versioned scorecard payload — pipeline and baseline side-by-side."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "baseline_metrics": baseline,
        "thresholds": THRESHOLDS,
        "gates": gates,
        "prompt_versions": prompt_versions,
        "prompt_overrides": overrides,
        "cases": results,
        "mode": "offline (stub LLM + fixture tools)",
    }


def _write_trend(
    metrics: dict[str, float],
    baseline: dict[str, float],
    results: list[dict[str, Any]],
) -> None:
    def _f(v: float, digits: int = 3) -> str:
        return f"{v:.{digits}f}"

    lines = [
        "# EDD Trend History",
        "",
        "| Run (UTC) | citation_support | rubric_avg | convergence | blocked "
        "| base_support | base_rubric | base_cost_usd |",
        "|---|---|---|---|---|---|---|---|",
        (
            f"| {datetime.now(UTC).isoformat()} | "
            f"{_f(metrics['citation_support_rate'])} | "
            f"{_f(metrics['rubric_score_avg'], 2)} | "
            f"{_f(metrics['debate_convergence_rate'])} | "
            f"{_f(metrics['blocked_rate'])} | "
            f"{_f(baseline['citation_support_rate'])} | "
            f"{_f(baseline['rubric_score_avg'], 2)} | "
            f"{baseline['avg_cost_usd']:.6f} |"
        ),
        "",
        "## Latest case results",
        "",
    ]
    for r in results:
        b = r["baseline"]
        lines.append(
            f"- `{r['case_id']}` — status={r['status']}, "
            f"support={r['citation_support']:.2f}, score={r['rubric_score']:.1f}, "
            f"rounds={r['debate_rounds']}, cost=${r['cost_usd']:.6f} "
            f"| baseline: support={b['citation_support']:.2f}, "
            f"score={b['rubric_score']:.1f}, cost=${b['cost_usd']:.6f}"
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
    baseline = aggregate_baseline(results)
    gates = _check_gates(metrics)

    prompt_versions: dict[str, int] = {}
    for r in results:
        prompt_versions.update(r.get("prompt_versions", {}))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scorecard = build_scorecard(
        metrics, baseline, gates, results, prompt_versions, overrides
    )
    SCORECARD_PATH.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
    if args.report:
        _write_trend(metrics, baseline, results)

    if args.langsmith:
        from evaluation.langsmith_eval import run_langsmith_experiment

        name = await asyncio.to_thread(run_langsmith_experiment)
        if name:
            print(f"LangSmith experiment published: {name}")
        else:
            print("LangSmith skipped: LANGCHAIN_API_KEY not configured")

    _print_scorecard(metrics, baseline, gates)
    failed = [k for k, ok in gates.items() if not ok]
    if failed:
        print(f"FAILED gates: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("All EDD gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
