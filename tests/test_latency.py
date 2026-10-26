"""Phase 8 — fetch cache, early-exit debate, citation shortcircuit."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.core.latency import LatencyTracker
from app.graph.graph import build_graph, initial_state
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.state import Brief
from app.services.cache_store import CacheStore
from app.services.llm_client import StubLLM
from app.services.scraper_client import ScraperClient
from app.services.search_client import (
    CachingSearchClient,
    SearchResult,
)
from tests.test_graph import _FakeScraper, _FakeSearch, _stub_llm


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, scrape_delay_seconds=0.0,
                    scrape_respect_robots=False, **kw)


class _CountingSearch:
    def __init__(self) -> None:
        self.calls = 0

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        self.calls += 1
        return [
            SearchResult(title="t", url="https://s.test/a", snippet="s")
            for _ in range(5)
        ]


async def test_cache_store_hit_miss_and_ttl(tmp_path: Any) -> None:
    store = CacheStore(str(tmp_path / "c.sqlite"))
    now = datetime.now(UTC)
    await store.put("scrape", "ws-a", "https://x", "{}", now=now)
    assert await store.get("scrape", "ws-a", "https://x", ttl_hours=24, now=now) == "{}"
    # Different key / day → miss.
    assert await store.get("scrape", "ws-a", "https://y", ttl_hours=24, now=now) is None
    # TTL expiry → miss + row pruned.
    later = now + timedelta(hours=25)
    assert await store.get(
        "scrape", "ws-a", "https://x", ttl_hours=24, now=later) is None


async def test_cache_tenant_isolation(tmp_path: Any) -> None:
    store = CacheStore(str(tmp_path / "c2.sqlite"))
    now = datetime.now(UTC)
    await store.put("scrape", "ws-a", "https://x", "secret-a", now=now)
    # Same URL, other workspace → never served across tenants.
    assert await store.get(
        "scrape", "ws-b", "https://x", ttl_hours=24, now=now) is None


async def test_search_cache_hit_miss_and_tenant(tmp_path: Any) -> None:
    inner = _CountingSearch()
    cache = CacheStore(str(tmp_path / "s.sqlite"))
    cached = CachingSearchClient(inner, cache, ttl_hours=24)
    await cached.search("q", max_results=3, workspace_id="ws-a")
    r2 = await cached.search("q", max_results=3, workspace_id="ws-a")
    assert inner.calls == 1  # second call served from cache
    assert len(r2) == 3
    # Other tenant does not hit ws-a's rows.
    await cached.search("q", max_results=3, workspace_id="ws-b")
    assert inner.calls == 2
    # No workspace → pass-through, no cache.
    await cached.search("q", max_results=3)
    assert inner.calls == 3


async def test_search_cache_needs_enough_results(tmp_path: Any) -> None:
    inner = _CountingSearch()
    cache = CacheStore(str(tmp_path / "s2.sqlite"))
    cached = CachingSearchClient(inner, cache, ttl_hours=24)
    await cached.search("q", max_results=2, workspace_id="ws")
    # Cached list covers the request → hit.
    await cached.search("q", max_results=4, workspace_id="ws")
    assert inner.calls == 1
    # Asking for more than cached → miss, refetch.
    await cached.search("q", max_results=6, workspace_id="ws")
    assert inner.calls == 2


class _CountingScraper(ScraperClient):
    def __init__(self, settings: Settings, cache: CacheStore) -> None:
        super().__init__(settings, cache=cache)
        self.fetches = 0

    async def _fetch(self, url: str) -> Any:
        self.fetches += 1

        class _R:
            status_code = 200
            headers = {"content-type": "text/html"}
            content = b"<html>cached body</html>"
            url = "https://x.test/p"

        return _R()


async def test_scraper_cache_hit_and_tenant(tmp_path: Any) -> None:
    settings = _settings()
    cache = CacheStore(str(tmp_path / "sc.sqlite"))
    scraper = _CountingScraper(settings, cache)
    p1 = await scraper.scrape("https://x.test/p", workspace_id="ws-a")
    p2 = await scraper.scrape("https://x.test/p", workspace_id="ws-a")
    assert scraper.fetches == 1
    assert p1.body == p2.body
    # Tenant isolation: ws-b refetches.
    await scraper.scrape("https://x.test/p", workspace_id="ws-b")
    assert scraper.fetches == 2


def _revising_stub(score: float) -> StubLLM:
    stub = _stub_llm(verdict="revise")
    stub.register(
        "ReviewOutput",
        lambda s, c, score=score: ReviewOutput(
            verdict="revise", score=score,
            rubric={"structure": score, "clarity": score,
                    "grounding": score, "tone": score},
            feedback=["minor polish"],
        ),
    )
    return stub


async def test_early_exit_high_score_skips_extra_round() -> None:
    """High rubric + clean citations → approve in one round, whatever the
    advisory verdict says (verdict is rubric-derived now)."""
    settings = _settings(review_early_exit_score=4.5, max_debate_rounds=3)
    graph = build_graph(
        settings, llm=_revising_stub(4.9),
        search=_FakeSearch(), scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )
    out = await graph.ainvoke(
        initial_state(
            job_id="ee", workspace_id="ws",
            brief=Brief(topic="early exit"),
            max_debate_rounds=settings.max_debate_rounds,
        ),
        config={"configurable": {"thread_id": "ee"}},
    )
    assert out["status"] == "done"
    assert out["latest_verdict"].verdict == "approve"
    # Only one debate round was needed.
    assert out["debate_round"] == 1


async def test_no_early_exit_below_threshold() -> None:
    settings = _settings(review_early_exit_score=4.5, max_debate_rounds=1)
    graph = build_graph(
        settings, llm=_revising_stub(3.0),
        search=_FakeSearch(), scraper=_FakeScraper(),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )
    out = await graph.ainvoke(
        initial_state(
            job_id="nee", workspace_id="ws",
            brief=Brief(topic="no early exit"),
            max_debate_rounds=settings.max_debate_rounds,
        ),
        config={"configurable": {"thread_id": "nee"}},
    )
    # Score below threshold → normal bound → escalate after round cap.
    assert out["status"] == "awaiting_review"


async def test_citation_shortcircuit_skips_llm_call() -> None:
    """A draft with unverifiable quotes → revise without burning tokens."""
    from app.graph.nodes.reviewer import ReviewerWorker

    class _SpyLLM(StubLLM):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def structured(self, schema: Any, **kw: Any) -> Any:
            self.calls += 1
            return await super().structured(schema, **kw)

    llm = _SpyLLM()
    worker = ReviewerWorker(llm, _prompts())
    from app.graph.state import Citation, EvidenceItem, ReportDraft

    draft = ReportDraft(
        version=1, title="t", markdown="m",
        citations=[Citation(claim="c", quote="never said this",
                            source_url="https://s.test/a")],
    )
    out = await worker.run({
        "brief": Brief(topic="shortcircuit test"),
        "drafts": [draft],
        "evidence": [EvidenceItem(
            evidence_id="e", url="https://s.test/a", text="different text",
        )],
        "debate_round": 0,
    })
    assert out["latest_verdict"].verdict == "revise"
    assert out["latest_verdict"].unsupported_citations == 1
    assert llm.calls == 0  # deterministic pre-check saved the whole call


def _prompts() -> Any:
    from app.services.prompt_registry import PromptRegistry

    return PromptRegistry.load()


def test_latency_tracker_quantiles() -> None:
    t = LatencyTracker()
    for v in [1.0, 2.0, 3.0, 100.0]:
        t.observe("writer", v)
    q = t.quantiles()["writer"]
    # Same nearest-rank convention as the trend history percentile.
    assert q["count"] == 4 and q["p50"] == 3.0 and q["p95"] == 100.0


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"
