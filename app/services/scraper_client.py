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
from collections.abc import Awaitable, Callable
from typing import Any

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
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


class _RobotsUnreachable(Exception):
    """robots.txt fetch failed server-side (5xx) — policy decides."""


def _bad_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Any address a server-side fetch must never connect to.

    IPv4-mapped IPv6 is unwrapped first so ``::ffff:10.0.0.1`` is judged
    by its real target. ``not is_global`` is the catch-all behind the
    explicit flags — it rejects carrier-grade NAT (100.64.0.0/10),
    benchmarking ranges and any future non-public block the flag set
    does not name.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


class _VettedDialBackend(httpcore.AsyncNetworkBackend):
    """Dial the DNS answers it vetted — closes the resolve→connect TOCTOU.

    httpcore calls ``connect_tcp(host, port)`` with the *origin hostname*
    the moment a new connection is needed. This backend resolves there
    (``_resolve_addresses``, the same seam tests fake), re-vets every
    answer and dials the vetted literal IP — so a hostile domain flipping
    DNS between the request-time check and the actual connect still hits
    the veto. The request URL is never rewritten: ``Host``, TLS SNI and
    certificate hostname verification all keep the original hostname.
    ``ScrapeError`` propagates unwrapped (it is not a ``ConnectError``),
    so callers see the same exception as the request-time gate.
    """

    def __init__(
        self,
        resolve: Callable[[str], Awaitable[list[str]]],
        *,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolve = resolve
        self._inner = inner if inner is not None else AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:  # literal IP — no DNS, still goes through the veto
            ipaddress.ip_address(host)
            candidates = [host]
        except ValueError:
            candidates = await self._resolve(host)
        vetted: list[str] = []
        for raw in candidates:
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                raise ScrapeError(f"host {host!r} resolved to {raw!r}") from None
            if _bad_address(ip):
                raise ScrapeError(
                    f"host {host!r} resolves to disallowed address {raw}"
                )
            vetted.append(raw)
        if not vetted:
            raise ScrapeError(f"host {host!r} resolved to no addresses")
        last_exc: OSError | None = None
        for addr in vetted:
            try:
                return await self._inner.connect_tcp(
                    addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except OSError as exc:
                last_exc = exc
        raise ScrapeError(
            f"cannot connect to any resolved address for {host!r}"
        ) from last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _VettedHTTPTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` whose pool dials through ``_VettedDialBackend``.

    Subclassing keeps respx/VCR-style mocks working (they patch the
    pool's ``handle_async_request`` upstream of the backend) while every
    real connection goes through the vetted dial. The pool is built
    without ``trust_env`` proxy support on purpose: an HTTP(S)_PROXY env
    var would hand the request — including DNS — to the proxy and bypass
    the SSRF guard entirely.
    """

    def __init__(
        self,
        backend: httpcore.AsyncNetworkBackend,
        *,
        http2: bool = False,
        limits: httpx.Limits,
    ) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            http1=True,
            http2=http2,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=backend,
        )


class ScraperClient:
    """Async fetcher enforcing politeness, robots.txt and SSRF guards."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache: CacheStoreProtocol | None = None,
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
        try:  # SSRF guard, literal IPs: reject non-public addresses
            ip = ipaddress.ip_address(parsed.hostname)
            if self._bad_address(ip):
                raise ScrapeError(f"disallowed address: {parsed.hostname}")
        except ValueError:
            pass  # hostname, not an IP literal — DNS-checked below
        return url

    _bad_address = staticmethod(_bad_address)

    async def _resolve_addresses(self, host: str) -> list[str]:
        """DNS resolution — a single seam so tests can fake the resolver."""
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, proto=6)  # TCP only
        return sorted({str(info[4][0]) for info in infos})

    async def _check_destination(self, url: str) -> str:
        """Full SSRF gate for a fetch target: scheme + literal IP + DNS.

        A hostname is resolved and rejected when *any* returned address
        is non-public — a public-looking host can still resolve to
        loopback, link-local metadata endpoints or private ranges.

        This is the *early* gate — it gives a clear ``ScrapeError`` before
        any network I/O. The connect-time gate is authoritative:
        ``_VettedDialBackend`` re-resolves inside ``connect_tcp`` and
        dials only the vetted literal IP, so a hostile domain cannot
        flip DNS answers between this check and the real connection
        (DNS-rebinding / TOCTOU closed; Host/SNI stay the hostname).
        """
        url = self._validate_url(url)
        host = urllib.parse.urlparse(url).hostname or ""
        try:
            ipaddress.ip_address(host)
            return url  # literal already vetted by _validate_url
        except ValueError:
            pass
        try:
            addrs = await self._resolve_addresses(host)
        except OSError as exc:
            raise ScrapeError(f"cannot resolve {host!r}: {exc}") from exc
        for raw in addrs:
            try:
                if self._bad_address(ipaddress.ip_address(raw)):
                    raise ScrapeError(
                        f"host {host!r} resolves to disallowed address {raw}"
                    )
            except ValueError:
                raise ScrapeError(f"host {host!r} resolved to {raw!r}") from None
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
                        if resp.status_code >= 500:
                            raise _RobotsUnreachable(f"HTTP {resp.status_code}")
                        if resp.status_code >= 400:
                            # RFC 9309: 4xx → no robots.txt → allow.
                            self._robots[origin] = None
                            logger.info(
                                "robots_txt_absent",
                                origin=origin, status=resp.status_code,
                            )
                            return True
                        candidate.parse(resp.text.splitlines())
                    except _RobotsUnreachable as exc:
                        self._robots[origin] = None
                        allowed = (
                            self._settings.scrape_robots_fail_policy == "allow"
                        )
                        logger.info(
                            "robots_txt_unreachable",
                            origin=origin, reason=str(exc), policy=allowed,
                        )
                        return allowed
                    except Exception as exc:  # timeout/transport → policy
                        self._robots[origin] = None
                        allowed = (
                            self._settings.scrape_robots_fail_policy == "allow"
                        )
                        logger.info(
                            "robots_txt_unreachable",
                            origin=origin, reason=repr(exc), policy=allowed,
                        )
                        return allowed
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
                        transport=_VettedHTTPTransport(
                            _VettedDialBackend(self._resolve_addresses),
                            http2=_HTTP2,
                            limits=httpx.Limits(
                                max_connections=20, max_keepalive_connections=10
                            ),
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
        """GET with manual redirect following — every hop re-validated.

        ``follow_redirects=True`` would let a 30x bounce the fetch to an
        unchecked host (e.g. public page redirecting to 169.254.169.254).
        Each ``Location`` instead goes through ``_check_destination``
        (scheme + literal IP + DNS) before the next request, capped at
        ``scrape_max_redirects`` hops.
        """
        headers = {"User-Agent": _USER_AGENT}
        client = await self._shared_client()
        current = await self._check_destination(url)
        for _hop in range(self._settings.scrape_max_redirects + 1):
            resp = await client.get(
                current, headers=headers, follow_redirects=False
            )
            if not resp.is_redirect:
                return resp
            location = resp.headers.get("location", "")
            if not location:
                return resp
            current = await self._check_destination(
                urllib.parse.urljoin(current, location)
            )
        raise ScrapeError(
            f"redirect limit ({self._settings.scrape_max_redirects}) "
            f"exceeded for {url}"
        )

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

        With ``workspace_id`` + a ``CacheStoreProtocol``, successful fetches are
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
