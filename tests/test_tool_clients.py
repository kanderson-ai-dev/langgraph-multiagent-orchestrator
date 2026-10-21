"""Tool clients: search, scraper, document parser — fully mocked."""

from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.services.document_parser import parse_document
from app.services.scraper_client import (
    ScrapeError,
    ScraperClient,
    _VettedDialBackend,
)
from app.services.search_client import (
    DuckDuckGoSearchClient,
    NullSearchClient,
    TavilySearchClient,
    build_search_client,
)

# ─── Search ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_tavily_search_maps_results() -> None:
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "A", "url": "https://a.test", "content": "snip a"},
                    {"title": "B", "url": "https://b.test", "content": "snip b"},
                ]
            },
        )
    )
    client = TavilySearchClient("tvly-test")
    out = await client.search("q", max_results=2)
    assert [r.title for r in out] == ["A", "B"]
    assert out[0].url == "https://a.test"


async def test_null_search_returns_empty() -> None:
    assert await NullSearchClient().search("anything", max_results=3) == []


def test_build_search_client_resolution() -> None:
    none = Settings(_env_file=None, search_provider="none")
    assert isinstance(build_search_client(none), NullSearchClient)

    tavily = Settings(
        _env_file=None, search_provider="tavily", search_api_key="k"
    )
    assert isinstance(build_search_client(tavily), TavilySearchClient)

    ddg = Settings(_env_file=None, search_provider="duckduckgo")
    assert isinstance(build_search_client(ddg), DuckDuckGoSearchClient)

    # auto without key → duckduckgo; auto with key → tavily
    auto_no_key = Settings(_env_file=None, search_provider="auto")
    assert isinstance(build_search_client(auto_no_key), DuckDuckGoSearchClient)


# ─── Scraper ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def scraper_settings() -> Settings:
    return Settings(
        _env_file=None,
        scrape_delay_seconds=0.0,
        scrape_respect_robots=False,
        scrape_max_bytes=1024,
    )


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: Any) -> None:
    """Default: every hostname resolves to a public IP — tests stay
    offline and only override when the case is about DNS itself."""
    async def fake(self: ScraperClient, host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ScraperClient, "_resolve_addresses", fake)


@respx.mock
async def test_scraper_fetches_and_caps_bytes(scraper_settings: Settings) -> None:
    respx.get("https://ok.test/page").mock(
        return_value=httpx.Response(
            200, content=b"x" * 5000, headers={"content-type": "text/html"}
        )
    )
    s = ScraperClient(scraper_settings)
    page = await s.scrape("https://ok.test/page")
    assert page.status_code == 200
    assert len(page.body) == 1024  # capped


@respx.mock
async def test_scraper_rejects_bad_status(scraper_settings: Settings) -> None:
    respx.get("https://bad.test/404").mock(return_value=httpx.Response(404))
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError):
        await s.scrape("https://bad.test/404")


async def test_scraper_rejects_disallowed_scheme(scraper_settings: Settings) -> None:
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError):
        await s.scrape("file:///etc/passwd")
    with pytest.raises(ScrapeError):
        await s.scrape("ftp://x.test/f")


async def test_scraper_blocks_private_ip_literals(scraper_settings: Settings) -> None:
    s = ScraperClient(scraper_settings)
    for url in ("http://127.0.0.1/x", "http://10.0.0.1/y", "http://169.254.169.254/meta"):
        with pytest.raises(ScrapeError):
            await s.scrape(url)


# ─── SSRF via DNS (Phase 17) ─────────────────────────────────────────────────

_PUBLIC_IP = "93.184.216.34"  # example.com-ish; only the value matters


def _dns(monkeypatch: Any, addrs: list[str]) -> None:
    async def fake(self: ScraperClient, host: str) -> list[str]:
        return addrs

    monkeypatch.setattr(ScraperClient, "_resolve_addresses", fake)


async def test_scraper_blocks_hostname_resolving_to_loopback(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, ["127.0.0.1"])
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError, match="disallowed address"):
        await s.scrape("https://sneaky.test/x")


async def test_scraper_blocks_hostname_resolving_to_metadata(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, ["169.254.169.254"])
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError, match="disallowed address"):
        await s.scrape("https://sneaky.test/meta")


async def test_scraper_blocks_when_any_resolved_address_is_private(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    # One public + one private A record → still refused.
    _dns(monkeypatch, [_PUBLIC_IP, "10.1.2.3"])
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError, match="disallowed address"):
        await s.scrape("https://sneaky.test/x")


@respx.mock
async def test_scraper_allows_hostname_resolving_public(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/p").mock(return_value=httpx.Response(200))
    s = ScraperClient(scraper_settings)
    page = await s.scrape("https://ok.test/p")
    assert page.status_code == 200


@respx.mock
async def test_scraper_blocks_redirect_to_private_ip(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/go").mock(
        return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest"}
        )
    )
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError):
        await s.scrape("https://ok.test/go")


@respx.mock
async def test_scraper_blocks_redirect_to_dns_private_host(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, ["10.9.9.9"])
    respx.get("https://ok.test/go").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://internal.test/secret"}
        )
    )
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError, match="disallowed address"):
        await s.scrape("https://ok.test/go")


@respx.mock
async def test_scraper_follows_public_redirects_within_limit(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/a").mock(
        return_value=httpx.Response(
            301, headers={"location": "https://ok.test/b"}
        )
    )
    respx.get("https://ok.test/b").mock(
        return_value=httpx.Response(200, content=b"final")
    )
    s = ScraperClient(scraper_settings)
    page = await s.scrape("https://ok.test/a")
    assert page.status_code == 200 and page.url == "https://ok.test/b"


@respx.mock
async def test_scraper_redirect_limit(
    scraper_settings: Settings, monkeypatch: Any
) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    for i in range(5):
        respx.get(f"https://ok.test/r{i}").mock(
            return_value=httpx.Response(
                302, headers={"location": f"https://ok.test/r{i+1}"}
            )
        )
    s = ScraperClient(scraper_settings)
    with pytest.raises(ScrapeError, match="redirect limit"):
        await s.scrape("https://ok.test/r0")


# ─── DNS-rebinding TOCTOU (connect to the vetted IP) ─────────────────────────


class _FakeInner:
    """Records what the dial backend actually connects to."""

    def __init__(self) -> None:
        self.dialed: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Any = None,
        local_address: Any = None,
        socket_options: Any = None,
    ) -> object:
        self.dialed.append((host, port))
        return object()  # opaque stream stand-in

    async def connect_unix_socket(self, path: str, **kw: Any) -> object:
        raise AssertionError("unix sockets are not used by the scraper")

    async def sleep(self, seconds: float) -> None:
        return None


def _flip_resolver(answers: list[list[str]]) -> Any:
    calls = iter(answers)

    async def fake(host: str) -> list[str]:
        return next(calls)

    return fake


async def test_backend_dials_vetted_ip_not_hostname() -> None:
    inner = _FakeInner()
    backend = _VettedDialBackend(_flip_resolver([[_PUBLIC_IP]]), inner=inner)
    stream = await backend.connect_tcp("ok.test", 443)
    assert stream is not None
    # The socket targets the vetted literal IP — the hostname only lives
    # in the request URL (Host header, TLS SNI, cert verification).
    assert inner.dialed == [(_PUBLIC_IP, 443)]


async def test_rebinding_second_resolution_rejected() -> None:
    inner = _FakeInner()
    backend = _VettedDialBackend(
        _flip_resolver([[_PUBLIC_IP], ["127.0.0.1"]]), inner=inner
    )
    await backend.connect_tcp("sneaky.test", 443)
    assert inner.dialed == [(_PUBLIC_IP, 443)]
    with pytest.raises(ScrapeError, match="disallowed address"):
        await backend.connect_tcp("sneaky.test", 443)
    # loopback was never dialed — second resolution was vetoed at connect
    assert inner.dialed == [(_PUBLIC_IP, 443)]


async def test_backend_rejects_mixed_answers() -> None:
    inner = _FakeInner()
    backend = _VettedDialBackend(
        _flip_resolver([[_PUBLIC_IP, "10.9.9.9"]]), inner=inner
    )
    with pytest.raises(ScrapeError, match="disallowed address"):
        await backend.connect_tcp("mixed.test", 443)
    assert inner.dialed == []


async def test_backend_rejects_nonpublic_addresses() -> None:
    inner = _FakeInner()
    for bad in (
        "::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "100.64.0.1",  # carrier-grade NAT
        "198.18.0.1",  # benchmarking range
    ):
        backend = _VettedDialBackend(_flip_resolver([[bad]]), inner=inner)
        with pytest.raises(ScrapeError):
            await backend.connect_tcp("evil.test", 443)
    assert inner.dialed == []


async def test_backend_vets_ip_literals_without_dns() -> None:
    inner = _FakeInner()

    async def explode(host: str) -> list[str]:
        raise AssertionError("resolver must not run for IP literals")

    backend = _VettedDialBackend(explode, inner=inner)
    with pytest.raises(ScrapeError, match="disallowed address"):
        await backend.connect_tcp("169.254.169.254", 80)
    assert inner.dialed == []


async def test_backend_public_literal_dialed_directly() -> None:
    inner = _FakeInner()

    async def explode(host: str) -> list[str]:
        raise AssertionError("resolver must not run for IP literals")

    backend = _VettedDialBackend(explode, inner=inner)
    await backend.connect_tcp(_PUBLIC_IP, 80)
    assert inner.dialed == [(_PUBLIC_IP, 80)]


async def test_backend_empty_resolution_rejected() -> None:
    backend = _VettedDialBackend(_flip_resolver([[]]), inner=_FakeInner())
    with pytest.raises(ScrapeError, match="no addresses"):
        await backend.connect_tcp("gone.test", 443)


async def test_shared_client_uses_vetted_transport(
    scraper_settings: Settings,
) -> None:
    s = ScraperClient(scraper_settings)
    client = await s._shared_client()
    backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
    assert isinstance(backend, _VettedDialBackend)


async def test_bad_address_flags_cgnat_and_mapped() -> None:
    import ipaddress

    for bad in (
        "100.64.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "0.0.0.0",
        "::",
    ):
        assert ScraperClient._bad_address(ipaddress.ip_address(bad)), bad
    assert not ScraperClient._bad_address(ipaddress.ip_address("8.8.8.8"))


# ─── robots.txt failure policy (Phase 17) ────────────────────────────────────


def _robots_settings(**kw: Any) -> Settings:
    base = {
        "_env_file": None,
        "scrape_delay_seconds": 0.0,
        "scrape_respect_robots": True,
        "scrape_max_bytes": 1024,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


@respx.mock
async def test_robots_4xx_allows_fetch(monkeypatch: Any) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/robots.txt").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://ok.test/p").mock(return_value=httpx.Response(200))
    s = ScraperClient(_robots_settings())
    assert (await s.scrape("https://ok.test/p")).status_code == 200


@respx.mock
async def test_robots_5xx_default_policy_denies(monkeypatch: Any) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/robots.txt").mock(
        return_value=httpx.Response(503)
    )
    s = ScraperClient(_robots_settings())  # default policy: deny (RFC 9309)
    with pytest.raises(ScrapeError, match="robots"):
        await s.scrape("https://ok.test/p")


@respx.mock
async def test_robots_5xx_allow_policy_still_fetches(monkeypatch: Any) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/robots.txt").mock(
        return_value=httpx.Response(503)
    )
    respx.get("https://ok.test/p").mock(return_value=httpx.Response(200))
    s = ScraperClient(_robots_settings(scrape_robots_fail_policy="allow"))
    assert (await s.scrape("https://ok.test/p")).status_code == 200


@respx.mock
async def test_robots_5xx_deny_policy_blocks(monkeypatch: Any) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/robots.txt").mock(
        return_value=httpx.Response(503)
    )
    s = ScraperClient(_robots_settings(scrape_robots_fail_policy="deny"))
    with pytest.raises(ScrapeError, match="robots"):
        await s.scrape("https://ok.test/p")


@respx.mock
async def test_robots_timeout_obeys_deny_policy(monkeypatch: Any) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/robots.txt").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )
    s = ScraperClient(_robots_settings(scrape_robots_fail_policy="deny"))
    with pytest.raises(ScrapeError, match="robots"):
        await s.scrape("https://ok.test/p")


@respx.mock
async def test_robots_timeout_obeys_allow_policy(monkeypatch: Any) -> None:
    _dns(monkeypatch, [_PUBLIC_IP])
    respx.get("https://ok.test/robots.txt").mock(
        side_effect=httpx.ConnectTimeout("boom")
    )
    respx.get("https://ok.test/p").mock(return_value=httpx.Response(200))
    s = ScraperClient(_robots_settings(scrape_robots_fail_policy="allow"))
    assert (await s.scrape("https://ok.test/p")).status_code == 200


# ─── Document parser ─────────────────────────────────────────────────────────


def test_parse_html_strips_scripts_and_extracts_text() -> None:
    html = (
        b"<html><head><title>T</title><script>alert(1)</script></head>"
        b"<body><h1>Hello</h1><p>World content</p></body></html>"
    )
    doc = parse_document(html, content_type="text/html", url="https://x.test")
    assert doc.kind == "html"
    assert doc.title == "T"
    assert "Hello" in doc.text and "World content" in doc.text
    assert "alert(1)" not in doc.text


def test_parse_pdf_extracts_text() -> None:
    # Build a valid one-page PDF with real text using pypdf itself.
    import io

    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Hello PDF World) Tj ET")
    page = writer.pages[0]
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)

    doc = parse_document(
        buf.getvalue(), content_type="application/pdf", url="https://x.test/f.pdf"
    )
    assert doc.kind == "pdf"
    assert "Hello" in doc.text


def test_parse_document_dispatches_on_magic_bytes() -> None:
    # Corrupt PDF must not raise — degrades to an unparseable doc.
    doc = parse_document(b"%PDF-broken", content_type="", url="u")
    assert doc.kind == "unparseable"
    assert doc.text == ""
