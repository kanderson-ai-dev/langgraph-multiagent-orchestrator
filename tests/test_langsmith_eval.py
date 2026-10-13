"""LangSmith experiment wiring — fully offline, no langsmith import.

The experiment function short-circuits before importing the SDK when a
required key is missing, so these tests exercise configuration, evaluators
and the target factory deterministically.
"""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.prompt_registry import PromptRegistry
from evaluation import langsmith_eval as le
from evaluation.dataset import DATASET


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0, **kw)  # type: ignore[arg-type]


def test_experiment_skipped_without_langsmith_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    # Prevent _settings_with_env from re-injecting a .env key via
    # configure_tracing (os.environ.setdefault on a real Settings()).
    monkeypatch.setattr(le, "_settings_with_env", _settings)
    assert le.run_langsmith_experiment() is None
    assert le.run_langsmith_experiment(live=True) is None


def test_live_experiment_requires_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGCHAIN_API_KEY", "ls-test-key")
    # Settings without an OpenAI key — live mode must refuse early.
    monkeypatch.setattr(le, "_settings_with_env", _settings)
    assert le.run_langsmith_experiment(live=True) is None


def test_evaluators_cover_scorecard_metrics() -> None:
    """Every gated per-case metric reaches the experiment as a score."""
    specs = le._evaluator_specs()
    run = SimpleNamespace(
        outputs={
            "citation_support": 1.0,
            "rubric_score": 4.5,
            "judge_score": 4.0,
            "converged": True,
            "blocked": True,
        }
    )
    example = SimpleNamespace(outputs={"expect_blocked": True})
    scores = {e(run, example)["key"]: e(run, example)["score"] for e in specs}
    assert scores == {
        "citation_support": 1.0,
        "rubric_score": 4.5,
        "judge_score": 4.0,
        "converged": True,
        "blocked_correctly": 1.0,
    }


def test_blocked_evaluator_flags_mismatch() -> None:
    run = SimpleNamespace(outputs={"blocked": False})
    example = SimpleNamespace(outputs={"expect_blocked": True})
    assert le._blocked_evaluator(run, example)["score"] == 0.0


def test_experiment_metadata_carries_provenance() -> None:
    registry = PromptRegistry.load()
    meta = le._experiment_metadata(_settings(), registry, live=True)
    assert meta["mode"].startswith("live")
    assert meta["dataset"] == le.DATASET_NAME
    assert meta["judge_model"] == _settings().judge_model
    assert meta["git_sha"]
    # Every registry prompt name contributes a promoted version.
    assert set(meta["prompt_versions"]) == set(registry.names())
    assert all(isinstance(v, int) for v in meta["prompt_versions"].values())


def test_offline_metadata_mode_label() -> None:
    meta = le._experiment_metadata(
        _settings(), PromptRegistry.load(), live=False
    )
    assert "stub" in meta["mode"]


def test_live_target_factory_returns_metrics() -> None:
    """Live factory wires real clients — with no key they degrade to the
    safe fallbacks (stub LLM, null search), still yielding a metrics dict.
    """
    settings = _settings()
    registry = PromptRegistry.load()
    target = le._target_factory(settings, registry, live=True)
    out = target(
        {
            "case_id": DATASET[0].case_id,
            "topic": DATASET[0].topic,
            "audience": DATASET[0].audience,
            "tone": DATASET[0].tone,
            "requirements": DATASET[0].requirements,
        }
    )
    for key in (*le._SCORE_KEYS, "blocked", "cost_usd"):
        assert key in out
