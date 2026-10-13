"""Optional LangSmith integration — publish the EDD dataset as an experiment.

The deterministic offline gate in ``run_eval.py`` stays the source of truth
for CI. When ``LANGCHAIN_API_KEY`` is configured, ``--langsmith`` also
uploads the dataset and runs the same cases through ``langsmith.evaluate()``
so each run shows up under LangSmith "Datasets & Experiments" — including
per-example scores and linked traces.

Two experiment flavours:

- **offline** (``--langsmith``): stub LLM + fixture tools — deterministic,
  zero cost, used to sanity-check the harness end to end.
- **live** (``--langsmith-live``): real ``OpenAIStructuredLLM`` generator,
  real ``JUDGE_MODEL`` judge, real search provider and scraper — the real
  metrics a human reads off the experiment table. Requires
  ``OPENAI_API_KEY``.

Usage:
    uv run python -m evaluation.run_eval --langsmith
    uv run python -m evaluation.run_eval --langsmith-live
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any

from app.core.config import Settings
from app.main import configure_tracing
from app.services.llm_client import build_judge_llm, build_llm
from app.services.prompt_eval import brief_for_case, run_eval_case
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import build_search_client
from evaluation.dataset import DATASET, EvalCase
from evaluation.run_eval import _run_case

DATASET_NAME = "langgraph-orchestrator-edd"
OFFLINE_PREFIX = "edd-offline"
LIVE_PREFIX = "edd-live"

# Score keys each example exposes to the evaluators — mirrors the
# per-case metrics in the scorecard (judge included).
_SCORE_KEYS = ["citation_support", "rubric_score", "judge_score", "converged"]


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


def _target_factory(
    settings: Settings, registry: PromptRegistry, *, live: bool
) -> Any:
    """Replay each example through the real graph.

    Offline: stub LLM + fixture tools. Live: the production clients —
    real OpenAI generator and judge, real search provider and scraper —
    so experiment scores are the actual measured metrics.
    """
    if live:
        llm = build_llm(settings)
        judge_llm = build_judge_llm(settings)
        search = build_search_client(settings)
        scraper = ScraperClient(settings)

        def target(inputs: dict[str, Any]) -> dict[str, Any]:
            case = EvalCase.model_validate(inputs)
            metrics, _state = asyncio.run(
                run_eval_case(
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
            )
            return metrics

        return target

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        case = EvalCase.model_validate(inputs)
        return asyncio.run(_run_case(case, settings, registry))

    return target


def _metric(key: str) -> Any:
    def evaluator(run: Any, example: Any) -> dict[str, Any]:
        return {"key": key, "score": run.outputs.get(key, 0.0)}

    return evaluator


def _blocked_evaluator(run: Any, example: Any) -> dict[str, Any]:
    expected = bool((example.outputs or {}).get("expect_blocked", False))
    actual = bool(run.outputs.get("blocked", False))
    return {"key": "blocked_correctly", "score": float(actual == expected)}


def _evaluator_specs() -> list[Any]:
    return [_metric(k) for k in _SCORE_KEYS] + [_blocked_evaluator]


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _experiment_metadata(
    settings: Settings, registry: PromptRegistry, *, live: bool
) -> dict[str, Any]:
    """Provenance for the experiment row — same fields as history.jsonl."""
    return {
        "mode": (
            "live (real OpenAI + search/scrape)"
            if live
            else "offline (stub LLM + fixture tools)"
        ),
        "dataset": DATASET_NAME,
        "git_sha": _git_sha(),
        "model": settings.chat_model_name,
        "judge_model": settings.judge_model,
        "prompt_versions": {
            name: registry.get(name).version for name in registry.names()
        },
    }


def run_langsmith_experiment(*, live: bool = False) -> str | None:
    """Upload the dataset and run a LangSmith experiment.

    Returns the experiment name, or ``None`` when no LangSmith key is
    configured — the offline gate result is unaffected either way.
    ``live=True`` additionally requires ``OPENAI_API_KEY`` and spends
    real tokens; never use it in the mandatory CI path.
    """
    settings = _settings_with_env()
    if not os.environ.get("LANGCHAIN_API_KEY"):
        return None
    if live and settings.openai_api_key is None:
        print(
            "live experiment requires OPENAI_API_KEY", file=sys.stderr
        )
        return None

    from langsmith import Client
    from langsmith.evaluation import evaluate

    client = Client()
    registry = PromptRegistry.load()  # promoted YAML prompts
    dataset = _ensure_dataset(client)
    experiment = evaluate(
        _target_factory(settings, registry, live=live),
        data=dataset,
        evaluators=_evaluator_specs(),
        experiment_prefix=LIVE_PREFIX if live else OFFLINE_PREFIX,
        metadata=_experiment_metadata(settings, registry, live=live),
        max_concurrency=1,
    )
    return getattr(experiment, "experiment_name", None) or str(experiment)
