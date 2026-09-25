"""Shared pytest fixtures and credential-gated markers.

The suite must run fully offline by default: no network calls and no real
API keys. Tests that legitimately need live credentials are marked with
``requires_*`` markers and skip automatically when the secret is absent.
"""

import os
from collections.abc import Iterator

import pytest
from _pytest.config import Config
from _pytest.nodes import Item

from app.core.config import Settings

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


@pytest.fixture()
def settings() -> Iterator[Settings]:
    """Isolated settings for tests — no .env file, no real credentials."""
    yield Settings(_env_file=None)


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
