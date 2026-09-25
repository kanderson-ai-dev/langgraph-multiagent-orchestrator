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


class ParsedDocument(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    kind: str = "unknown"


def _clean(text: str) -> str:
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_html(body: bytes, url: str) -> ParsedDocument:
    soup = BeautifulSoup(body, "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = _clean(soup.get_text("\n"))
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
