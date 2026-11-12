"""Shared pytest fixtures and credential-gated markers.

The suite must run fully offline by default: no network calls and no real
API keys. Tests that legitimately need live credentials are marked with
``requires_*`` markers and skip automatically when the secret is absent.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from _pytest.config import Config
from _pytest.nodes import Item

from app.core.config import Settings
from app.services.prompt_eval import _template_cover_markdown
from app.services.prompt_registry import PromptRegistry

# Marker -> env var that must be present (and non-empty) for the test to run.
_MARKERS: dict[str, str] = {
    "requires_openai_key": "OPENAI_API_KEY",
    "requires_search_api": "SEARCH_API_KEY",
    "requires_langsmith_key": "LANGCHAIN_API_KEY",
}


def pytest_collection_modifyitems(config: Config, items: list[Item]) -> None:  # noqa: ARG001
    for item in items:
        for marker, env_var in _MARKERS.items():
            if item.get_closest_marker(marker) and not os.environ.get(env_var):
                item.add_marker(
                    pytest.mark.skip(reason=f"{env_var} not configured — skipping live test")
                )


@pytest.fixture(autouse=True)
def _isolate_audit_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test-sealed jobs must not append to the real ``data/audit_anchor.jsonl``.

    ``Settings`` reads the env var even with ``_env_file=None``, so every
    test-built runner picks up the per-test path automatically."""
    monkeypatch.setenv("AUDIT_ANCHOR_PATH", str(tmp_path / "audit_anchor.jsonl"))


@pytest.fixture(autouse=True)
def _gc_collect_on_teardown() -> Iterator[None]:
    """Force a GC pass at every teardown.

    ``sqlite3.Connection.__del__`` reports "unclosed database" wherever
    the collector happens to run — without this fixture that is a random
    later test (the historical flake). Collecting at teardown keeps the
    warning inside the test that leaked the connection.
    """
    yield
    import gc

    gc.collect()


@pytest.fixture()
def settings() -> Iterator[Settings]:
    """Isolated settings for tests — no .env file, no real credentials."""
    yield Settings(_env_file=None)


@pytest.fixture(scope="session")
def prompts() -> PromptRegistry:
    """Prompt Registry loaded from the repo's YAML seeds (read-only)."""
    return PromptRegistry.load()


# Stub-draft markdown containing every section of every SECTION_TEMPLATES
# entry as `##` headings — the mechanical structure check in the Reviewer
# matches required headings as an ordered subsequence, so this draft shape
# satisfies every report_type at once.
ALL_SECTIONS_MARKDOWN = _template_cover_markdown()


@pytest.fixture()
def brief_kwargs() -> dict[str, object]:
    """Minimal valid report-brief fields shared across tests."""
    return {
        "topic": "Adoption of retrieval-augmented generation in mid-market SaaS",
        "audience": "CTO and engineering leadership",
        "tone": "analytical",
        "requirements": ["Executive summary", "Competitive landscape", "Adoption risks"],
        "source_urls": [],
        "language": "en",
    }
