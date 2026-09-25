"""Deterministic offline evaluation harness — the EDD quality gate.

Runs the dataset through the real compiled graph with a stubbed LLM and
fixture-based search/scrape (zero network, zero credentials). Exits non-zero
if any versioned threshold fails — the same gate CI enforces.

Usage:
    uv run python -m evaluation.run_eval            # evaluate + write scorecard
    uv run python -m evaluation.run_eval --report   # also refresh TREND.md
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.writer import DraftCitation, DraftOutput
from app.graph.state import Brief
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScrapedPage
from app.services.search_client import SearchResult
from evaluation.dataset import DATASET, EvalCase
from evaluation.evaluators import (
    blocked_correctly,
    citation_support,
    debate_converged,
    rubric_score,
)

RESULTS_DIR = Path(__file__).parent / "results"
SCORECARD_PATH = RESULTS_DIR / "scorecard.json"
TREND_PATH = RESULTS_DIR / "TREND.md"

# Thresholds — identical to the project objective table.
THRESHOLDS = {
    "citation_support_rate": 0.90,
    "rubric_score_avg": 4.0,
    "debate_convergence_rate": 0.85,
    "blocked_rate": 1.0,
}

_FIXTURE_HTML = (
    b"<html><title>Evidence</title><body>"
    b"Enterprises adopting retrieval augmented generation report measurable "
    b"grounding improvements and reduced hallucination rates in production."
    b"</body></html>"
)


class _FixtureSearch:
    """Fixture-based search — deterministic, offline."""

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"Source for {query[:40]}",
                url="https://eval.fixture/source",
                snippet="fixture evidence",
            )
        ]


class _FixtureScraper:
    async def scrape(self, url: str) -> ScrapedPage:
        return ScrapedPage(
            url=url, status_code=200, content_type="text/html", body=_FIXTURE_HTML
        )


def _eval_llm() -> StubLLM:
    """Deterministic outputs: grounded draft citing fixture text, approve."""
    stub = StubLLM()
    stub.register(
        "ResearchPlan",
        lambda s, c: ResearchPlan(sub_questions=["q"], queries=["eval query"]),
    )
    stub.register(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="Evaluation Report",
            markdown=(
                "## Summary\nEnterprises adopting retrieval augmented "
                "generation report measurable grounding improvements."
            ),
            citations=[
                DraftCitation(
                    claim="Grounding improves with retrieval",
                    quote=(
                        "Enterprises adopting retrieval augmented generation "
                        "report measurable grounding improvements"
                    ),
                    source_url="https://eval.fixture/source",
                )
            ],
        ),
    )
    stub.register(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.5,
            rubric={"structure": 4.5, "clarity": 4.5, "grounding": 4.5, "tone": 4.5},
        ),
    )
    return stub


async def _run_case(case: EvalCase, settings: Settings) -> dict[str, Any]:
    graph = build_graph(
        settings,
        llm=_eval_llm(),
        search=_FixtureSearch(),  # type: ignore[arg-type]
        scraper=_FixtureScraper(),  # type: ignore[arg-type]
    )
    state = initial_state(
        job_id=f"eval-{case.case_id}",
        workspace_id="eval",
        brief=Brief(
            topic=case.topic,
            audience=case.audience,
            tone=case.tone,
            requirements=case.requirements,
        ),
        max_debate_rounds=settings.max_debate_rounds,
    )
    out = await graph.ainvoke(state)
    return {
        "case_id": case.case_id,
        "status": out.get("status"),
        "citation_support": citation_support(out),
        "rubric_score": rubric_score(out),
        "converged": debate_converged(out),
        "blocked": blocked_correctly(out),
        "expected_blocked": case.expect_blocked,
        "debate_rounds": out.get("debate_round", 0),
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    normal = [r for r in results if not r["expected_blocked"]]
    blocked = [r for r in results if r["expected_blocked"]]
    return {
        "citation_support_rate": (
            sum(r["citation_support"] for r in normal) / len(normal) if normal else 0.0
        ),
        "rubric_score_avg": (
            sum(r["rubric_score"] for r in normal) / len(normal) if normal else 0.0
        ),
        "debate_convergence_rate": (
            sum(1 for r in normal if r["converged"]) / len(normal) if normal else 0.0
        ),
        "blocked_rate": (
            sum(1 for r in blocked if r["blocked"]) / len(blocked) if blocked else 1.0
        ),
    }


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


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="write TREND.md")
    args = parser.parse_args()

    settings = Settings(
        _env_file=None, scrape_delay_seconds=0.0, scrape_respect_robots=False
    )
    results = [await _run_case(c, settings) for c in DATASET]
    metrics = _aggregate(results)
    gates = _check_gates(metrics)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scorecard = {
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "gates": gates,
        "cases": results,
        "mode": "offline (stub LLM + fixture tools)",
    }
    SCORECARD_PATH.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
    if args.report:
        _write_trend(metrics, results)

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
