"""Replay recorded live interactions — the real pipeline, fully offline.

Each cassette captures one dataset case run against the real OpenAI +
DuckDuckGo + scraper path (recorded by
``evaluation/record_cassettes.py``). Replaying with ``record_mode="none"``
fails on any unrecorded request, so these tests are guaranteed offline —
CI never needs credentials to see the live path work end to end.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.graph.graph import build_graph, initial_state
from app.services.llm_client import OpenAIStructuredLLM
from app.services.prompt_eval import brief_for_case
from app.services.prompt_registry import PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import SearchResult
from evaluation.dataset import DATASET
from evaluation.record_cassettes import (
    CASES_TO_RECORD,
    cassette_for,
    search_sidecar_path,
)

CASSETTE_DIR = Path(__file__).parent / "cassettes"


class _ReplaySearch:
    """Search client replaying the recorded ``query -> results`` sidecar.

    ``ddgs`` runs over ``curl_cffi`` — outside vcrpy's reach — so the
    recorded result sets (captured by ``record_cassettes.py``) stand in
    for it. Outcomes are consumed per call, in order: the Nth replayed
    call to a query sees the Nth recorded outcome — a query that failed
    mid-recording fails here too instead of leaking a later success.
    Unrecorded queries return empty, deterministically.
    """

    def __init__(self, sidecar: Path) -> None:
        self._queries: dict[str, list[list[dict[str, Any]]]] = {}
        self._calls: dict[str, int] = {}
        if sidecar.exists():
            raw = json.loads(sidecar.read_text("utf-8")).get("queries", {})
            for q, hits in raw.items():
                # Current format: list of per-call outcome sets. A legacy
                # flat ``query -> [hits]`` entry normalizes to one outcome.
                self._queries[q] = (
                    [hits] if hits and isinstance(hits[0], dict) else hits
                )

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        idx = self._calls.get(query, 0)
        self._calls[query] = idx + 1
        outcomes = self._queries.get(query) or []
        hits = outcomes[idx] if idx < len(outcomes) else []
        return [SearchResult(**h) for h in hits[:max_results]]


def _replay_settings() -> Settings:
    # A fake key is fine — vcr intercepts every httpx call, nothing leaves.
    return Settings(
        _env_file=None,
        openai_api_key="test-replay-key",
        scrape_delay_seconds=0.0,
    )


@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param(
            c,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "VCR cassettes were recorded against the pre-upgrade "
                    "prompt shapes (first-1500-chars evidence, no "
                    "previous-draft block). They are re-recorded against "
                    "the final stack in the upgrade plan's Phase 8."
                ),
            ),
        )
        if c != "blocked-injection"
        # Guardrail-blocked before any LLM call — its cassette is prompt-
        # shape independent, so it still replays green today.
        else c
        for c in CASES_TO_RECORD
    ],
)
async def test_recorded_case_replays_offline(
    case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette = CASSETTE_DIR / f"edd_{case_id}.yaml"
    if not cassette.exists():
        pytest.skip(f"cassette {cassette.name} not recorded")
    case = next(c for c in DATASET if c.case_id == case_id)
    settings = _replay_settings()
    # The scraper's SSRF DNS check must not hit the network in replay —
    # vcrpy serves the recorded response for every hostname anyway.
    async def _fake_resolve(self: Any, host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ScraperClient, "_resolve_addresses", _fake_resolve)

    # stdlib robotparser rules differ across Python versions; the cassette
    # was recorded where every page was allowed, so pin that decision here.
    async def _allow_all(self: Any, url: str) -> bool:
        return True

    monkeypatch.setattr(ScraperClient, "_robots_allowed", _allow_all)
    graph = build_graph(
        settings,
        llm=OpenAIStructuredLLM(settings),
        search=_ReplaySearch(search_sidecar_path(case_id)),
        scraper=ScraperClient(settings),
        prompts=PromptRegistry.load(),
    )
    state = initial_state(
        job_id=f"replay-{case_id}",
        workspace_id="eval",
        brief=brief_for_case(case),
        max_debate_rounds=settings.max_debate_rounds,
    )
    with cassette_for(case_id, record_mode="none"):
        out = await graph.ainvoke(state)

    if case.expect_blocked:
        assert out["status"] == "blocked"
    else:
        assert out["status"] in ("done", "awaiting_review")
        assert out["evidence"], "live run must gather real evidence"
        assert out["drafts"], "live run must produce a draft"
