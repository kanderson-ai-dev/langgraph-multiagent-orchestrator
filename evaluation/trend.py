"""EDD trend history — append-only JSONL store + TREND.md renderer.

Every ``run_eval`` invocation appends one row to
``evaluation/results/history.jsonl`` capturing the full run context:
timestamp, git SHA, prompt versions, generator/judge model names, gated
metrics, baseline metrics, cost and latency percentiles. ``TREND.md`` is
regenerated from the history so the table is a projection of durable
data, not a hand-edited artifact.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings

RESULTS_DIR = Path(__file__).parent / "results"
HISTORY_PATH = RESULTS_DIR / "history.jsonl"
TREND_PATH = RESULTS_DIR / "TREND.md"

# Columns rendered in TREND.md — gated metrics first, then informational.
_METRIC_COLS = [
    ("citation_support_rate", "citation", 3),
    ("rubric_score_avg", "rubric", 2),
    ("judge_score_avg", "judge", 2),
    ("debate_convergence_rate", "converge", 3),
    ("blocked_rate", "blocked", 3),
]


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(len(ordered) * pct)))
    return ordered[idx]


def build_history_row(
    *,
    metrics: dict[str, float],
    baseline: dict[str, float],
    results: list[dict[str, Any]],
    prompt_versions: dict[str, int],
    settings: Settings,
    mode: str,
) -> dict[str, Any]:
    """One append-only history record for an eval run."""
    normal = [r for r in results if not r["expected_blocked"]]
    latencies = [r["duration_seconds"] for r in normal]
    return {
        "run_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "mode": mode,
        "model": settings.chat_model_name,
        "judge_model": settings.judge_model,
        "prompt_versions": prompt_versions,
        "metrics": metrics,
        "baseline_metrics": baseline,
        "avg_cost_usd": metrics.get("avg_cost_usd", 0.0),
        "judge_cost_usd": metrics.get("judge_cost_usd", 0.0),
        "baseline_cost_usd": baseline.get("avg_cost_usd", 0.0),
        "p50_latency_seconds": _percentile(latencies, 0.50),
        "p95_latency_seconds": _percentile(latencies, 0.95),
        "cases": len(results),
    }


def append_history(row: dict[str, Any], path: Path = HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def read_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _pv_cell(prompt_versions: dict[str, int]) -> str:
    return ",".join(f"{k}@v{v}" for k, v in sorted(prompt_versions.items()))


def render_trend(
    rows: list[dict[str, Any]],
    latest_results: list[dict[str, Any]] | None = None,
    path: Path = TREND_PATH,
) -> None:
    """Regenerate TREND.md from history (most recent last)."""
    header = (
        "| Run (UTC) | SHA | mode | prompts | model | judge_model |"
        + "".join(f" {label} |" for _key, label, _d in _METRIC_COLS)
        + " cost_avg | judge_cost | p50_s | p95_s | base_support | base_cost |"
    )
    lines = [
        "# EDD Trend History",
        "",
        "Append-only history in `history.jsonl`; this file is regenerated",
        "from it — do not hand-edit.",
        "",
        header,
        "|---" * (header.count("|") - 1) + "|",
    ]
    for row in rows:
        cells = []
        for key, _label, digits in _METRIC_COLS:
            value = row.get("metrics", {}).get(key)
            cells.append(f"{value:.{digits}f}" if isinstance(value, float) else "n/a")
        base_support = row.get("baseline_metrics", {}).get("citation_support_rate")
        base_cell = f"{base_support:.3f}" if isinstance(base_support, float) else "n/a"
        judge_cost = row.get("judge_cost_usd")
        judge_cell = f"{judge_cost:.6f}" if isinstance(judge_cost, float) else "n/a"
        lines.append(
            f"| {row['run_at']} | {row.get('git_sha', '?')} | {row.get('mode', '?')} "
            f"| {_pv_cell(row.get('prompt_versions', {}))} "
            f"| {row.get('model', '?')} | {row.get('judge_model', '?')} "
            f"| {' | '.join(cells)} "
            f"| {row.get('avg_cost_usd', 0.0):.6f} "
            f"| {judge_cell} "
            f"| {row.get('p50_latency_seconds', 0.0):.2f} "
            f"| {row.get('p95_latency_seconds', 0.0):.2f} "
            f"| {base_cell} | {row.get('baseline_cost_usd', 0.0):.6f} |"
        )
    if latest_results:
        lines += ["", "## Latest case results", ""]
        for r in latest_results:
            b = r.get("baseline", {})
            lines.append(
                f"- `{r['case_id']}` — status={r['status']}, "
                f"support={r['citation_support']:.2f}, "
                f"score={r['rubric_score']:.1f}, "
                f"judge={r.get('judge_score', 0.0):.1f}, "
                f"rounds={r['debate_rounds']}, "
                f"cost=${r['cost_usd']:.6f} "
                f"| baseline: support={b.get('citation_support', 0.0):.2f}, "
                f"score={b.get('rubric_score', 0.0):.1f}, "
                f"cost=${b.get('cost_usd', 0.0):.6f}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
