"""Optional LangSmith integration — publish the EDD dataset as an experiment.

The deterministic offline gate in ``run_eval.py`` stays the source of truth
for CI. When ``LANGCHAIN_API_KEY`` is configured, ``--langsmith`` also
uploads the dataset and runs the same cases through ``langsmith.evaluate()``
so each run shows up under LangSmith "Datasets & Experiments" — including
per-example scores and linked traces.

Usage:
    uv run python -m evaluation.run_eval --langsmith
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.core.config import Settings
from app.main import configure_tracing
from evaluation.dataset import DATASET, EvalCase
from evaluation.run_eval import _run_case

DATASET_NAME = "langgraph-orchestrator-edd"
EXPERIMENT_PREFIX = "edd-offline"


def _settings_with_env() -> Settings:
    """Real ``.env`` settings — the offline gate deliberately ignores them."""
    settings = Settings()
    configure_tracing(settings)  # export LANGCHAIN_* so Client()/tracers see them
    return settings


def _inputs(case: EvalCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "topic": case.topic,
        "audience": case.audience,
        "tone": case.tone,
        "requirements": case.requirements,
    }


def _ensure_dataset(client: Any) -> Any:
    """Create the dataset once; reuse it on subsequent runs (idempotent)."""
    if client.has_dataset(dataset_name=DATASET_NAME):
        return client.read_dataset(dataset_name=DATASET_NAME)
    ds = client.create_dataset(
        dataset_name=DATASET_NAME,
        description=(
            "EDD evaluation briefs for the supervisor-workers orchestrator: "
            "quality cases plus one prompt-injection case that must be blocked."
        ),
    )
    client.create_examples(
        inputs=[_inputs(c) for c in DATASET],
        outputs=[{"expect_blocked": c.expect_blocked} for c in DATASET],
        dataset_id=ds.id,
    )
    return ds


def _target_factory(settings: Settings) -> Any:
    """Replay each example through the real graph (fixture tools, stub LLM)."""

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        case = EvalCase.model_validate(inputs)
        return asyncio.run(_run_case(case, settings))

    return target


def _metric(key: str) -> Any:
    def evaluator(run: Any, example: Any) -> dict[str, Any]:
        return {"key": key, "score": run.outputs.get(key, 0.0)}

    return evaluator


def _blocked_evaluator(run: Any, example: Any) -> dict[str, Any]:
    expected = bool((example.outputs or {}).get("expect_blocked", False))
    actual = bool(run.outputs.get("blocked", False))
    return {"key": "blocked_correctly", "score": float(actual == expected)}


def run_langsmith_experiment() -> str | None:
    """Upload the dataset and run a LangSmith experiment.

    Returns the experiment name, or ``None`` when no LangSmith key is
    configured — the offline gate result is unaffected either way.
    """
    settings = _settings_with_env()
    if not os.environ.get("LANGCHAIN_API_KEY"):
        return None

    from langsmith import Client
    from langsmith.evaluation import evaluate

    client = Client()
    dataset = _ensure_dataset(client)
    experiment = evaluate(
        _target_factory(settings),
        data=dataset,
        evaluators=[
            _metric("citation_support"),
            _metric("rubric_score"),
            _metric("converged"),
            _blocked_evaluator,
        ],
        experiment_prefix=EXPERIMENT_PREFIX,
        metadata={
            "mode": "offline (stub LLM + fixture tools)",
            "dataset": DATASET_NAME,
        },
        max_concurrency=1,
    )
    return getattr(experiment, "experiment_name", None) or str(experiment)
