"""Live latency benchmark — p50/p95 end-to-end over N dataset cases.

Runs real OpenAI + real search/scrape through the actual compiled graph
(the same ``run_eval_case`` the quality eval uses) and records the
result in ``evaluation/results/history.jsonl`` as a ``latency-bench``
row. This is the only sanctioned source for README latency numbers —
never hand-edit them.

Usage:
    uv run python -m evaluation.latency_bench          # default: all cases
    uv run python -m evaluation.latency_bench --cases 3 --reps 2

Requires OPENAI_API_KEY (exits 0 with a notice when absent — never fakes
numbers).
"""

import argparse
import asyncio
import sys

from app.core.config import Settings
from app.services.llm_client import build_judge_llm, build_llm
from app.services.prompt_eval import brief_for_case, run_eval_case
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import build_search_client
from evaluation.dataset import DATASET
from evaluation.trend import _percentile, append_history, build_history_row


async def _main() -> int:
    from app.core.logging import harden_console_streams

    harden_console_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=len(DATASET))
    parser.add_argument("--reps", type=int, default=1,
                        help="repetitions per case (percentiles over all runs)")
    args = parser.parse_args()

    settings = Settings()  # real env — this bench is live-only
    if settings.openai_api_key is None:
        print("latency_bench skipped: OPENAI_API_KEY not configured")
        return 0

    registry = PromptRegistry.load()
    llm = build_llm(settings)
    search = build_search_client(settings)
    scraper = ScraperClient(settings)
    judge_llm = build_judge_llm(settings)

    cases = DATASET[: max(1, args.cases)]
    results: list[dict[str, object]] = []
    for rep in range(args.reps):
        for case in cases:
            metrics, state = await run_eval_case(
                case_id=case.case_id,
                brief=brief_for_case(case),
                expect_blocked=case.expect_blocked,
                settings=settings,
                registry=registry,
                llm=llm,
                judge_llm=judge_llm,
                search=search,
                scraper=scraper,
            )
            metrics["rep"] = rep
            metrics["prompt_versions"] = state.get("prompt_versions", {})
            results.append(metrics)
            print(
                f"  {case.case_id} rep{rep}: status={metrics['status']} "
                f"t={metrics['duration_seconds']:.1f}s "
                f"cost=${metrics['cost_usd']:.4f}"
            )
    await scraper.aclose()

    latencies = [
        float(r["duration_seconds"])
        for r in results
        if not r["expected_blocked"]
    ]
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    costs = [float(r["cost_usd"]) for r in results]

    prompt_versions: dict[str, int] = {}
    for r in results:
        prompt_versions.update(r.get("prompt_versions", {}))  # type: ignore[arg-type]

    append_history(
        build_history_row(
            metrics={
                "avg_latency_seconds": sum(latencies) / len(latencies)
                if latencies else 0.0,
                "p50_latency_seconds": p50,
                "p95_latency_seconds": p95,
                "avg_cost_usd": sum(costs) / len(costs) if costs else 0.0,
            },
            baseline={},
            results=results,
            prompt_versions=prompt_versions,
            settings=settings,
            mode="latency-bench (live)",
        )
    )

    n = len(latencies)
    print(
        f"\nLatency over {n} timed runs "
        f"(HITL-paused cases measure active segments):"
    )
    print(f"  p50 = {p50:.1f}s   p95 = {p95:.1f}s   (gate: p50<=90s, p95<=240s)")
    verdict = "PASS" if p50 <= 90 and p95 <= 240 else "FAIL"
    print(f"  gate: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
