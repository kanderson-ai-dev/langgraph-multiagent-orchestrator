"""Single-prompt baseline: same-schema metrics, both columns in scorecard."""

import json

from app.core.config import Settings
from app.graph.state import Brief, EvidenceItem
from app.services.prompt_registry import PromptRegistry
from evaluation.baseline import aggregate_baseline, run_baseline_case
from evaluation.dataset import DATASET
from evaluation.run_eval import _run_case, build_scorecard

_FIXTURE_TEXT = (
    "Enterprises adopting retrieval augmented generation report measurable "
    "grounding improvements and reduced hallucination rates in production."
)

_SHARED_KEYS = {
    "case_id",
    "citation_support",
    "rubric_score",
    "cost_usd",
    "duration_seconds",
    "llm_calls",
}


def _settings() -> Settings:
    return Settings(
        _env_file=None, scrape_delay_seconds=0.0, scrape_respect_robots=False
    )


async def test_baseline_result_schema() -> None:
    """Baseline case metrics mirror the pipeline case schema."""
    evidence = [
        EvidenceItem(
            evidence_id="e",
            url="https://eval.fixture/source",
            title="fixture",
            text=_FIXTURE_TEXT,
        )
    ]
    result = await run_baseline_case(
        case_id="t",
        brief=Brief(topic="Any topic"),
        evidence=evidence,
        settings=_settings(),
        review_system="grade this draft",
    )
    assert set(result) >= _SHARED_KEYS
    # The stub ships one grounded + one fabricated quote — nothing verifies.
    assert result["citation_support"] == 0.5
    assert result["llm_calls"] == 1
    assert result["produced_report"] is True
    assert result["cost_usd"] > 0
    assert result["duration_seconds"] >= 0


async def test_pipeline_case_carries_baseline_and_cost() -> None:
    """_run_case returns pipeline metrics plus the baseline side-by-side."""
    registry = PromptRegistry.load()
    result = await _run_case(DATASET[0], _settings(), registry)
    assert set(result) >= _SHARED_KEYS
    assert set(result["baseline"]) >= _SHARED_KEYS
    assert result["llm_calls"] > result["baseline"]["llm_calls"]
    assert result["citation_support"] == 1.0
    assert result["baseline"]["citation_support"] == 0.5


def test_aggregate_baseline_columns() -> None:
    results = [
        {
            "expected_blocked": False,
            "baseline": {
                "citation_support": 0.5,
                "rubric_score": 4.5,
                "cost_usd": 0.001,
                "duration_seconds": 0.1,
                "llm_calls": 1,
                "produced_report": True,
            },
        },
        {
            "expected_blocked": True,
            "baseline": {
                "citation_support": 0.0,
                "rubric_score": 4.5,
                "cost_usd": 0.001,
                "duration_seconds": 0.1,
                "llm_calls": 1,
                "produced_report": True,  # no guardrail — it produced anyway
            },
        },
    ]
    agg = aggregate_baseline(results)
    assert agg["citation_support_rate"] == 0.5
    assert agg["blocked_rate"] == 0.0  # never refuses — honest safety gap
    assert agg["avg_llm_calls"] == 1.0


def test_scorecard_contains_both_columns() -> None:
    card = build_scorecard(
        metrics={"citation_support_rate": 1.0},
        baseline={"citation_support_rate": 0.5},
        gates={"citation_support_rate": True},
        results=[{"case_id": "x", "baseline": {"citation_support": 0.5}}],
        prompt_versions={"writer": 1},
        overrides={},
        settings=_settings(),
        mode="offline",
    )
    assert card["metrics"]["citation_support_rate"] == 1.0
    assert card["baseline_metrics"]["citation_support_rate"] == 0.5
    assert card["cases"][0]["baseline"]["citation_support"] == 0.5
    json.dumps(card)  # serializable — the scorecard is persisted verbatim


def test_baseline_is_informational_not_a_gate() -> None:
    """Baseline columns must never feed the threshold gates."""
    from app.services.prompt_eval import EDD_THRESHOLDS

    card = build_scorecard(
        metrics=dict.fromkeys(EDD_THRESHOLDS, 1.0),
        baseline={"citation_support_rate": 0.0},
        gates=dict.fromkeys(EDD_THRESHOLDS, True),
        results=[],
        prompt_versions={},
        overrides={},
        settings=_settings(),
        mode="offline",
    )
    assert set(card["gates"]) == set(EDD_THRESHOLDS)
    assert all(card["gates"].values())
