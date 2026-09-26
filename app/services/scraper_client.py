"""Polite, robots.txt-respecting HTTP fetcher for evidence gathering.

Safety: http/https only, per-domain politeness delay, byte cap, timeout,
optional robots.txt compliance. All fetched content is *untrusted data* —
sanitization happens in the guardrail, never trust it as instructions.
"""

import asyncio
import ipaddress
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

logger = get_logger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_USER_AGENT = "langgraph-multiagent-orchestrator/0.1 (+https://github.com/kanderson-ai-dev)"


class ScrapedPage(BaseModel):
    url: str
    status_code: int
    content_type: str
    body: bytes


class ScrapeError(Exception):
    """Raised when a URL cannot or must not be fetched."""


class ScraperClient:
    """Async fetcher enforcing politeness, robots.txt and SSRF guards."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
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
                        await asyncio.wait_for(
                            asyncio.to_thread(candidate.read),
                            timeout=self._settings.scrape_timeout_seconds,
                        )
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

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _fetch(self, url: str) -> httpx.Response:
        headers = {"User-Agent": _USER_AGENT}
        if self._client is not None:
            return await self._client.get(url, headers=headers, follow_redirects=True)
        async with httpx.AsyncClient(
            timeout=self._settings.scrape_timeout_seconds
        ) as client:
            return await client.get(url, headers=headers, follow_redirects=True)

    async def scrape(self, url: str) -> ScrapedPage:
        """Fetch ``url`` with robots.txt + politeness + size cap."""
        url = self._validate_url(url)
        if not await self._robots_allowed(url):
            raise ScrapeError(f"robots.txt disallows {url}")
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
        return ScrapedPage(
            url=str(resp.url), status_code=resp.status_code,
            content_type=content_type, body=body,
        )
