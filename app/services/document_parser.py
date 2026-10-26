"""Document parser — extracts clean text from HTML and PDF bytes.

Output is *untrusted data*: the caller (a graph node) must pass it through
the sanitization guardrail before it can reach any prompt.
"""

import io
import re

from bs4 import BeautifulSoup
from pydantic import BaseModel
from pypdf import PdfReader

_HTML_TYPES = {"text/html", "application/xhtml+xml", ""}
_PDF_TYPES = {"application/pdf"}
_WS_RE = re.compile(r"[ \t]+")

# Chrome that is never the article: structural tags plus class/id patterns
# typical of cookie bars, menus, promos and sidebar rails.
_BOILER_TAGS = ("nav", "header", "footer", "aside", "form", "iframe")
_BOILER_RE = re.compile(
    r"cookie|consent|banner|menu|sidebar|nav-|navbar|footer|header|"
    r"promo|advert|subscribe|newsletter|modal|popup|breadcrumb|social|share",
    re.IGNORECASE,
)


class ParsedDocument(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    kind: str = "unknown"


def _clean(text: str) -> str:
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_html(body: bytes, url: str) -> ParsedDocument:
    """Main-content extraction: chrome/boilerplate removed, ``<article>`` or
    ``<main>`` preferred when it actually holds the text (fallback: the
    boilerplate-stripped body)."""
    soup = BeautifulSoup(body, "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    for tag in soup(_BOILER_TAGS):
        tag.decompose()
    for attr in ("class", "id", "role"):
        for el in soup.find_all(attrs={attr: _BOILER_RE}):
            el.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("article") or soup.find("main")
    root = main if main and len(main.get_text(strip=True)) >= 200 else soup
    text = _clean(root.get_text("\n"))
    return ParsedDocument(url=url, title=title, text=text, kind="html")


def parse_pdf(body: bytes, url: str) -> ParsedDocument:
    reader = PdfReader(io.BytesIO(body))
    text = _clean("\n".join(page.extract_text() or "" for page in reader.pages))
    meta = reader.metadata
    title = ""
    if meta and meta.title:
        title = str(meta.title)
    return ParsedDocument(url=url, title=title, text=text, kind="pdf")


def parse_document(body: bytes, *, content_type: str, url: str) -> ParsedDocument:
    """Dispatch on content type; corrupt input degrades to an empty doc."""
    ct = content_type.lower()
    try:
        if ct in _PDF_TYPES or body[:5] == b"%PDF-":
            return parse_pdf(body, url)
        return parse_html(body, url)
    except Exception:
        return ParsedDocument(url=url, kind="unparseable")
