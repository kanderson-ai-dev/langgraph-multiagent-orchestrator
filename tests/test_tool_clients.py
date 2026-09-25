"""Tool clients: search, scraper, document parser — fully mocked."""

import httpx
import pytest
import respx

from app.core.config import Settings
from app.services.document_parser import parse_document
from app.services.scraper_client import ScrapeError, ScraperClient
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
