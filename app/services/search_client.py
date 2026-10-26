"""Web search client — Tavily (paid) or keyless DuckDuckGo fallback.

The ``none`` provider returns empty results so the system degrades cleanly
when search is disabled entirely.
"""

import asyncio
import base64
import binascii
import json
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.store_protocols import CacheStoreProtocol

logger = get_logger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


def _unwrap_ad_redirect(url: str) -> str:
    """Resolve ad-redirect result URLs to their real target.

    ``ddgs``'s Yahoo/Bing backends return ``bing.com/aclick`` hops whose
    ``u`` parameter carries the destination as base64 prefixed with ``a1``
    (or ``a2``). Scraping the hop itself is pointless — robots.txt refuses
    it — so unwrap it; when unwrapping fails the result is unusable and
    dropped (empty string)."""
    if "/aclick" not in url:
        return url
    token = parse_qs(urlparse(url).query).get("u", [""])[0]
    if len(token) > 2 and token[:2] in ("a1", "a2"):
        try:
            decoded = base64.b64decode(token[2:] + "===").decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""
        return decoded if decoded.startswith("http") else ""
    return ""


@runtime_checkable
class SearchClient(Protocol):
    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]: ...


class TavilySearchClient:
    def __init__(self, api_key: str, *, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                _TAVILY_URL,
                json={
                    "api_key": self._api_key,
                    "query": query,
                    "max_results": max_results,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            SearchResult(
                title=str(r.get("title", "")),
                url=str(r.get("url", "")),
                snippet=str(r.get("content", "")),
            )
            for r in data.get("results", [])
        ]


class DuckDuckGoSearchClient:
    """Keyless fallback via the ``ddgs`` package (sync lib → thread).

    The backend list is bounded on purpose: ``backend="auto"`` rotates through
    every engine ``ddgs`` knows, which added tens of seconds per query.
    """

    _BACKENDS = "duckduckgo,brave,yahoo"

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        def _sync_search() -> list[SearchResult]:
            from ddgs import DDGS

            with DDGS(timeout=int(self._timeout)) as ddgs:
                results = []
                for r in ddgs.text(
                    query, max_results=max_results, backend=self._BACKENDS
                ):
                    url = _unwrap_ad_redirect(str(r.get("href", "")))
                    if url:
                        results.append(
                            SearchResult(
                                title=str(r.get("title", "")),
                                url=url,
                                snippet=str(r.get("body", "")),
                            )
                        )
                return results

        return await asyncio.to_thread(_sync_search)


class NullSearchClient:
    """Provider ``none``: always returns no results."""

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        return []


class CachingSearchClient:
    """Per-tenant day-scoped search cache around any ``SearchClient``.

    A cached result list satisfies a request only when it covers the
    requested ``max_results`` (it is then truncated) — never returns
    another workspace's rows.
    """

    def __init__(
        self, inner: SearchClient, cache: CacheStoreProtocol, *, ttl_hours: float
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._ttl = ttl_hours

    async def search(
        self, query: str, *, max_results: int, workspace_id: str = ""
    ) -> list[SearchResult]:
        if not workspace_id:
            return await self._inner.search(
                query, max_results=max_results, workspace_id=workspace_id
            )
        key = " ".join(query.lower().split())
        raw = await self._cache.get(
            "search", workspace_id, key, ttl_hours=self._ttl
        )
        if raw is not None:
            try:
                results = [SearchResult.model_validate(r) for r in json.loads(raw)]
            except Exception:
                results = []
            if len(results) >= max_results:
                from app.core.metrics import CACHE_HITS_TOTAL

                CACHE_HITS_TOTAL.labels(kind="search", outcome="hit").inc()
                return results[:max_results]
        from app.core.metrics import CACHE_HITS_TOTAL

        CACHE_HITS_TOTAL.labels(kind="search", outcome="miss").inc()
        results = await self._inner.search(
            query, max_results=max_results, workspace_id=workspace_id
        )
        await self._cache.put(
            "search", workspace_id, key,
            json.dumps([r.model_dump() for r in results]),
        )
        return results


def build_search_client(
    settings: Settings, *, cache: CacheStoreProtocol | None = None
) -> SearchClient:
    provider = settings.effective_search_provider
    inner: SearchClient
    if provider == "tavily" and settings.search_api_key is not None:
        inner = TavilySearchClient(settings.search_api_key.get_secret_value())
    elif provider == "duckduckgo":
        inner = DuckDuckGoSearchClient(timeout=settings.scrape_timeout_seconds)
    else:
        if provider == "tavily":  # key vanished — degrade loudly-but-safely
            logger.warning(
                "search_provider=tavily but no SEARCH_API_KEY; using 'none'"
            )
        inner = NullSearchClient()
    if cache is not None and settings.cache_enabled:
        return CachingSearchClient(
            inner, cache, ttl_hours=settings.cache_ttl_hours
        )
    return inner
