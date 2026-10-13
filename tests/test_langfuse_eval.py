"""Langfuse experiment wiring — offline-safe, SDK client never required.

``run_langfuse_experiment`` must no-op to ``None`` whenever the provider is
not configured, so the EDD gates never depend on Langfuse being reachable.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from evaluation import langfuse_eval as lfe


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0, **kw)  # type: ignore[arg-type]


def test_experiment_skipped_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lfe, "_settings_with_env", _settings)
    monkeypatch.setattr(lfe, "_langfuse_client", lambda: None)
    assert lfe.run_langfuse_experiment() is None
    assert lfe.run_langfuse_experiment(live=True) is None


def test_live_experiment_requires_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lfe, "_settings_with_env", _settings)
    monkeypatch.setattr(lfe, "_langfuse_client", lambda: object())
    assert lfe.run_langfuse_experiment(live=True) is None


def test_dataset_item_ids_are_stable() -> None:
    """Idempotent dataset upload: the same case maps to the same item id."""
    from evaluation.dataset import DATASET

    ids = {f"{lfe.DATASET_NAME}:{c.case_id}" for c in DATASET}
    assert len(ids) == len(DATASET)
    assert all(i.startswith("langgraph-orchestrator-eval-v1:") for i in ids)
