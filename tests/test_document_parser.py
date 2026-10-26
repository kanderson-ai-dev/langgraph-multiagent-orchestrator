"""Main-content extraction — boilerplate off, real text kept."""

from app.services.document_parser import parse_html


def test_boilerplate_removed_and_article_preferred() -> None:
    body = b"""<html><head><title>T</title></head><body>
    <nav><a href="/">Home</a><a href="/pricing">Pricing menu link</a></nav>
    <div class="cookie-banner">We use cookies. Accept all?</div>
    <article><h1>RAG adoption grows</h1>
    <p>""" + (
        b"Enterprises adopting retrieval augmented generation report "
        b"measurable grounding gains. " * 8
    ) + b"""</p></article>
    <aside class="sidebar">Subscribe to our newsletter for updates!</aside>
    <footer>Copyright 2026 Example Corp. All rights reserved footer nav.</footer>
    </body></html>"""
    doc = parse_html(body, "https://news.example/a")
    assert "grounding gains" in doc.text
    assert "cookies" not in doc.text.lower()
    assert "newsletter" not in doc.text.lower()
    assert "Copyright" not in doc.text
    assert "Pricing menu link" not in doc.text


def test_fallback_when_no_article_or_short_article() -> None:
    body = b"""<html><body>
    <header>Site Header With Tagline</header>
    <div><p>""" + b"Vendor docs content with the real signal sentence here. " * 6 + b"""</p></div>
    <div id="promo-banner">Buy now limited offer!</div>
    </body></html>"""
    doc = parse_html(body, "https://docs.example/x")
    assert "real signal sentence" in doc.text
    assert "Tagline" not in doc.text
    assert "limited offer" not in doc.text


def test_marketing_page_chrome_stripped() -> None:
    body = b"""<html><body>
    <form>Sign up<input/></form>
    <main><h1>Platform</h1><p>""" + (
        b"SOC 2 reports available under NDA with details. " * 6
    ) + b"""</p></main>
    <div role="navigation">nav links</div>
    </body></html>"""
    doc = parse_html(body, "https://vendor.example/")
    assert "SOC 2 reports" in doc.text
    assert "Sign up" not in doc.text
    assert "nav links" not in doc.text
