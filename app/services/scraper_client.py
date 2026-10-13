"""Polite, robots.txt-respecting HTTP fetcher for evidence gathering.

Safety: http/https only, per-domain politeness delay, byte cap, timeout,
optional robots.txt compliance. All fetched content is *untrusted data* —
sanitization happens in the guardrail, never trust it as instructions.
"""

import asyncio
import base64
import ipaddress
import json
import time
import urllib.parse
import urllib.robotparser
from collections import defaultdict

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
from app.services.cache_store import CacheStore

logger = get_logger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_USER_AGENT = "langgraph-multiagent-orchestrator/0.1 (+https://github.com/kanderson-ai-dev)"

try:  # HTTP/2 is a latency optimization — degrade to h1 when h2 is absent.
    import h2 as _h2  # noqa: F401

    _HTTP2 = True
except ImportError:  # pragma: no cover - depends on env
    _HTTP2 = False


class ScrapedPage(BaseModel):
    url: str
    status_code: int
    content_type: str
    body: bytes


class ScrapeError(Exception):
    """Raised when a URL cannot or must not be fetched."""


class ScraperClient:
    """Async fetcher enforcing politeness, robots.txt and SSRF guards."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache: CacheStore | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()
        self._cache = cache
        self._last_fetch: dict[str, float] = defaultdict(float)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # Concurrency is safe: the semaphore bounds total in-flight fetches,
        # and per-domain/per-origin locks keep politeness spacing and the
        # robots cache correct when callers gather many URLs at once.
        self._semaphore = asyncio.Semaphore(settings.scrape_max_concurrency)
        self._domain_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _validate_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            raise ScrapeError(f"disallowed scheme: {parsed.scheme!r}")
        if not parsed.hostname:
            raise ScrapeError("URL has no hostname")
        try:  # SSRF guard: reject literal private/loopback IPs
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ScrapeError(f"disallowed address: {parsed.hostname}")
        except ValueError:
            pass  # hostname, not an IP literal
        return url

    async def _robots_allowed(self, url: str) -> bool:
        if not self._settings.scrape_respect_robots:
            return True
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            async with self._robots_locks[origin]:
                if origin not in self._robots:  # re-check inside the lock
                    candidate = urllib.robotparser.RobotFileParser()
                    candidate.set_url(f"{origin}/robots.txt")
                    try:
                        # Fetch through _fetch (httpx): same UA, timeout and
                        # redirect handling as page fetches — and visible to
                        # VCR cassettes for deterministic offline replay.
                        resp = await self._fetch(f"{origin}/robots.txt")
                        if resp.status_code >= 400:
                            raise ScrapeError(f"HTTP {resp.status_code}")
                        candidate.parse(resp.text.splitlines())
                    except Exception:  # unreadable robots.txt → allow, logged
                        self._robots[origin] = None
                        logger.info("robots_txt_unreadable", origin=origin)
                    else:
                        self._robots[origin] = candidate
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(_USER_AGENT, url)

    async def _polite_delay(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc
        delay = self._settings.scrape_delay_seconds
        # The per-domain lock makes same-domain calls queue properly — each
        # waits its turn instead of racing to update _last_fetch together.
        async with self._domain_locks[domain]:
            elapsed = time.monotonic() - self._last_fetch[domain]
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_fetch[domain] = time.monotonic()

    async def _shared_client(self) -> httpx.AsyncClient:
        """Lazily built pooled client — HTTP/2 + keep-alive reuse.

        One connection pool for the process instead of a fresh TLS session
        per page fetch; h1 fallback when ``h2`` is not installed.
        """
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=self._settings.scrape_timeout_seconds,
                        http2=_HTTP2,
                        limits=httpx.Limits(
                            max_connections=20, max_keepalive_connections=10
                        ),
                    )
        return self._client

    async def aclose(self) -> None:
        """Close the pooled client — only when this instance created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _fetch(self, url: str) -> httpx.Response:
        headers = {"User-Agent": _USER_AGENT}
        client = await self._shared_client()
        return await client.get(url, headers=headers, follow_redirects=True)

    async def _cache_get(self, url: str, workspace_id: str) -> ScrapedPage | None:
        if self._cache is None or not workspace_id:
            return None
        raw = await self._cache.get(
            "scrape", workspace_id, url,
            ttl_hours=self._settings.cache_ttl_hours,
        )
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return ScrapedPage(
                url=data["url"], status_code=int(data["status_code"]),
                content_type=data["content_type"],
                body=base64.b64decode(data["body_b64"]),
            )
        except Exception:
            return None

    async def _cache_put(
        self, url: str, workspace_id: str, page: ScrapedPage
    ) -> None:
        if self._cache is None or not workspace_id:
            return
        payload = json.dumps(
            {
                "url": page.url,
                "status_code": page.status_code,
                "content_type": page.content_type,
                "body_b64": base64.b64encode(page.body).decode(),
            }
        )
        await self._cache.put("scrape", workspace_id, url, payload)

    async def scrape(self, url: str, *, workspace_id: str = "") -> ScrapedPage:
        """Fetch ``url`` with robots.txt + politeness + size cap.

        With ``workspace_id`` + a ``CacheStore``, successful fetches are
        cached per ``(workspace_id, url, UTC day)`` — tenant-scoped, so no
        content ever crosses tenants. Robots.txt still gates every call,
        including cache hits (policy may have changed since the fetch).
        """
        url = self._validate_url(url)
        if not await self._robots_allowed(url):
            raise ScrapeError(f"robots.txt disallows {url}")
        if self._cache is not None and self._settings.cache_enabled:
            cached = await self._cache_get(url, workspace_id)
            if cached is not None:
                from app.core.metrics import CACHE_HITS_TOTAL

                CACHE_HITS_TOTAL.labels(kind="scrape", outcome="hit").inc()
                logger.info("scrape_cache_hit", url=url)
                return cached
            from app.core.metrics import CACHE_HITS_TOTAL

            CACHE_HITS_TOTAL.labels(kind="scrape", outcome="miss").inc()
        async with self._semaphore:
            await self._polite_delay(url)
            resp = await self._fetch(url)
        if resp.status_code >= 400:
            raise ScrapeError(f"HTTP {resp.status_code} for {url}")
        body = resp.content[: self._settings.scrape_max_bytes]
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        logger.info(
            "scraped_page", url=url, status=resp.status_code,
            bytes=len(body), content_type=content_type,
        )
        page = ScrapedPage(
            url=str(resp.url), status_code=resp.status_code,
            content_type=content_type, body=body,
        )
        if self._cache is not None and self._settings.cache_enabled:
            await self._cache_put(url, workspace_id, page)
        return page
