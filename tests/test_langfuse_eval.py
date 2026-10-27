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
    assert all(i.startswith("langgraph-orchestrator-eval-v2:") for i in ids)


def test_inputs_roundtrip_preserves_every_field() -> None:
    """Regression: dataset items must carry the full case contract.

    A dropped ``tenant_context``/``report_type``/``language`` once made
    injection cases run live as harmless briefs (blocked gate 0.818).
    """
    from evaluation.dataset import DATASET, EvalCase
    from evaluation.dataset_heldout import HELDOUT

    for case in DATASET + HELDOUT:
        restored = EvalCase.model_validate(lfe._inputs(case))
        for field in (
            "topic", "audience", "tone", "requirements",
            "report_type", "language", "tenant_context",
        ):
            assert getattr(restored, field) == getattr(case, field), (
                f"{case.case_id}: field {field} lost in dataset item input"
            )


def test_fetch_experiment_scores_reads_experiments_endpoint() -> None:
    """Recorded-fixture read: a Langfuse ``Experiment`` payload (the
    shape ``api.experiments.list`` returns) parses into score metrics —
    the legacy ``dataset_runs`` endpoint is never touched."""
    from types import SimpleNamespace

    # Recorded response shape of GET /experiments (v4 SDK).
    experiment_payload = {
        "id": "exp-1",
        "name": "run-abc123",
        "description": "live",
        "item_count": 7,
        "dataset_id": "ds-1",
        "metadata": {"mode": "live"},
        "scores": {
            "citation_support": 0.93,
            "rubric_score": 4.2,
            "judge_score": 4.1,
            "converged": 1.0,
        },
    }

    class _Resp:
        data = [SimpleNamespace(**experiment_payload)]

    class _Experiments:
        def list(self, **kwargs: object) -> object:
            assert kwargs["name"] == "run-abc123"
            return _Resp()

    class _Client:
        api = SimpleNamespace(experiments=_Experiments())

    scores = lfe.fetch_experiment_scores(_Client(), "run-abc123")
    assert scores == {
        "citation_support": 0.93,
        "rubric_score": 4.2,
        "judge_score": 4.1,
        "converged": 1.0,
    }


def test_fetch_experiment_scores_degrades() -> None:
    """No experiments resource, unknown run, or a raising API → None."""

    class _Client:
        pass

    assert lfe.fetch_experiment_scores(_Client(), "x") is None

    from types import SimpleNamespace

    class _Empty:
        def list(self, **kwargs: object) -> object:
            return SimpleNamespace(data=[])

    client = SimpleNamespace(api=SimpleNamespace(experiments=_Empty()))
    assert lfe.fetch_experiment_scores(client, "nope") is None

    class _Boom:
        def list(self, **kwargs: object) -> object:
            raise RuntimeError("api down")

    client = SimpleNamespace(api=SimpleNamespace(experiments=_Boom()))
    assert lfe.fetch_experiment_scores(client, "x") is None


def test_ensure_dataset_upserts_stale_items() -> None:
    """Items whose stored input no longer matches are re-uploaded."""
    from evaluation.dataset import DATASET

    created: list[dict[str, object]] = []

    class _DS:
        items = [
            type("It", (), {
                "metadata": {"case_id": DATASET[0].case_id},
                "input": {"case_id": DATASET[0].case_id, "topic": DATASET[0].topic},
            })()
        ]

    class _Client:
        def get_dataset(self, name: str) -> object:
            return _DS()

        def create_dataset_item(self, **kw: object) -> None:
            created.append(kw)

    lfe._ensure_dataset(_Client())
    # Item 0 has a stale (truncated) input → re-uploaded; the rest are new.
    assert len(created) == len(DATASET)
    assert all(
        "tenant_context" in (c["input"] or {}) for c in created
    )
