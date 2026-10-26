"""Langfuse experiment backend — the LangSmith experiment's fallback twin.

Active when ``TRACING_PROVIDER=langfuse`` and both ``LANGFUSE_*`` keys are
set (verified live by ``init_tracing`` at call time — the module itself
stays importable and silent without the SDK).

Mirrors ``langsmith_eval``: the same EDD dataset is uploaded as a Langfuse
dataset (idempotent, stable item ids), each case runs through the real
graph — stub+fixtures offline, real OpenAI/search/scraper/judge with
``live=True`` — and the per-case scorecard metrics land as Langfuse
scores on the item run. The local ``scorecard.json`` remains the source
of truth for EDD gates; Langfuse is only the viewer.

Usage:
    uv run python -m evaluation.run_eval --langfuse-live
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.services.llm_client import build_judge_llm, build_llm
from app.services.prompt_eval import brief_for_case, run_eval_case
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import build_search_client
from app.services.tracing import init_tracing
from evaluation.dataset import DATASET, EvalCase
from evaluation.run_eval import _run_case

# Versioned dataset name — bump when the item schema changes (v2 added
# report_type/language/tenant_context to every item's input).
DATASET_NAME = "langgraph-orchestrator-eval-v2"

# Per-case score keys pushed as Langfuse scores (NUMERIC).
_SCORE_KEYS = [
    "citation_support", "rubric_score", "judge_score", "converged",
    "hitl_resumes",
]


def _settings_with_env() -> Settings:
    """Real ``.env`` settings; activates the Langfuse client when configured."""
    settings = Settings()
    init_tracing(settings)  # exports LANGFUSE_* env vars + auth_check
    return settings


def _langfuse_client() -> Any | None:
    """SDK client when importable; ``None`` keeps every caller offline-safe."""
    try:
        from langfuse import get_client

        return get_client()
    except Exception:
        return None


def _inputs(case: EvalCase) -> dict[str, Any]:
    """Every field that affects the run must reach the dataset item.

    An earlier version dropped ``report_type``/``language``/``tenant_context``,
    so tenant-context injection cases ran live as harmless briefs and the
    blocked gate was never exercised (measured 0.818 vs required 1.0).
    """
    return {
        "case_id": case.case_id,
        "topic": case.topic,
        "audience": case.audience,
        "tone": case.tone,
        "requirements": case.requirements,
        "report_type": case.report_type,
        "language": case.language,
        "tenant_context": case.tenant_context,
    }


def _ensure_dataset(client: Any) -> Any:
    """Create the dataset once; top up missing items (idempotent)."""
    try:
        dataset = client.get_dataset(DATASET_NAME)
    except Exception:
        dataset = None
    if dataset is None:
        dataset = client.create_dataset(
            name=DATASET_NAME,
            description=(
                "EDD evaluation briefs for the supervisor-workers orchestrator: "
                "quality cases plus one prompt-injection case that must be blocked."
            ),
        )
    known = {
        (item.metadata or {}).get("case_id"): getattr(item, "input", None)
        for item in getattr(dataset, "items", [])
        if getattr(item, "metadata", None)
    }
    for case in DATASET:
        # Upsert on the stable item id: a case whose stored input differs
        # (schema grew, fields were dropped) is rewritten, not skipped.
        if known.get(case.case_id) == _inputs(case):
            continue
        client.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=f"{DATASET_NAME}:{case.case_id}",
            input=_inputs(case),
            expected_output={"expect_blocked": case.expect_blocked},
            metadata={"case_id": case.case_id},
        )
    return client.get_dataset(DATASET_NAME)


def _make_task(
    settings: Settings, registry: PromptRegistry, *, live: bool
) -> Any:
    """``task(*, item)`` — replay one dataset item through the real graph."""
    if live:
        llm = build_llm(settings)
        judge_llm = build_judge_llm(settings)
        search = build_search_client(settings)
        scraper = ScraperClient(settings)

        async def task(*, item: Any, **kwargs: Any) -> dict[str, Any]:
            case = EvalCase.model_validate(item.input)
            metrics, _state = await run_eval_case(
                case_id=case.case_id,
                brief=brief_for_case(case),
                expect_blocked=case.expect_blocked,
                settings=settings,
                registry=registry,
                llm=llm,
                judge_llm=judge_llm,
                search=search,
                scraper=scraper,
                auto_review=True,
            )
            return metrics

        return task

    async def task(*, item: Any, **kwargs: Any) -> dict[str, Any]:
        case = EvalCase.model_validate(item.input)
        return await _run_case(case, settings, registry)

    return task


def _metric(key: str) -> Any:
    """Item evaluator exposing one scorecard metric as a NUMERIC score."""
    from langfuse.experiment import Evaluation

    def evaluator(
        *,
        input: Any,
        output: Any,
        expected_output: Any = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> Any:
        out = output or {}
        # A correctly blocked case produces no draft/report — the quality
        # metrics are N/A there; emitting 0.0 would disguise a *success*
        # as a failure. ``blocked_correctly`` is its metric.
        if (expected_output or {}).get("expect_blocked"):
            return None
        score = out.get(key)
        if score is None:
            return None
        return Evaluation(
            name=key, value=float(score), data_type="NUMERIC"
        )

    return evaluator


def _blocked_evaluator(
    *,
    input: Any,
    output: Any,
    expected_output: Any = None,
    metadata: Any = None,
    **kwargs: Any,
) -> Any:
    from langfuse.experiment import Evaluation

    expected = bool((expected_output or {}).get("expect_blocked", False))
    actual = bool((output or {}).get("blocked", False))
    return Evaluation(
        name="blocked_correctly",
        value=actual == expected,
        data_type="BOOLEAN",
    )


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


def _run_name(registry: PromptRegistry) -> str:
    """``<writer@version>-<git sha>-<date>-<HHMM>`` — unique per run.

    Same-named runs merge into one Langfuse experiment view (stale errors
    from failed runs would linger), so the timestamp keeps each execution
    its own row while remaining A/B-comparable by prefix.
    """
    version = registry.get("writer").version
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    return f"writer-v{version}-{_git_sha()}-{stamp}"


def run_langfuse_experiment(
    *,
    live: bool = False,
    registry: PromptRegistry | None = None,
) -> str | None:
    """Upload the dataset and run a Langfuse experiment.

    Returns the experiment run name, or ``None`` when Langfuse is not
    configured — EDD gates never depend on Langfuse being reachable.
    ``live=True`` additionally requires ``OPENAI_API_KEY`` and spends
    real tokens; never use it in the mandatory CI path.

    Pass ``registry.with_overrides({...})`` to evaluate a pinned candidate
    against the promoted prompts — the run name picks up the pinned
    version so Langfuse compares candidate-vs-promoted side by side.
    """
    settings = _settings_with_env()
    client = _langfuse_client()
    if client is None:
        return None
    if live and settings.openai_api_key is None:
        print(
            "live experiment requires OPENAI_API_KEY", file=sys.stderr
        )
        return None

    registry = registry or PromptRegistry.load()  # promoted YAML prompts
    dataset = _ensure_dataset(client)
    result = client.run_experiment(
        name=DATASET_NAME,
        run_name=_run_name(registry),
        description=(
            "live (real OpenAI + search/scrape)"
            if live
            else "offline (stub LLM + fixture tools)"
        ),
        data=list(getattr(dataset, "items", [])),
        task=_make_task(settings, registry, live=live),
        evaluators=_evaluator_specs(),
        max_concurrency=1,
        metadata={
            "mode": "live" if live else "offline",
            "git_sha": _git_sha(),
            "model": settings.chat_model_name,
            "judge_model": settings.judge_model,
        },
    )
    return getattr(result, "run_name", None) or str(result)
