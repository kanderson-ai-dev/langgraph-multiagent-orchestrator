"""DOCX export — renders the final report markdown as a .docx download.

Minimal block renderer (headings, lists, paragraphs, bold runs) built on
``python-docx`` — a single pure-Python dependency already pulled in with
``lxml`` underneath. Anything richer would need pandoc; that trade-off is
deliberately not taken.
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.text.paragraph import Paragraph

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^(#{1,6})\s*")
_MD_LIST = re.compile(r"^[-*]\s+")


def _write_runs(paragraph: Paragraph, text: str) -> None:
    """Split on ``**bold**`` markers so emphasis survives the export."""
    for i, chunk in enumerate(_MD_BOLD.split(text)):
        if not chunk:
            continue
        run = paragraph.add_run(chunk)
        run.bold = i % 2 == 1


def markdown_to_docx(
    markdown: str, *, title: str = "Report", footer: str = ""
) -> bytes:
    """Convert markdown text to DOCX bytes (headings, lists, body text)."""
    doc = Document()
    doc.add_heading(title[:120], level=0)

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _MD_HEADING.match(stripped)
        if m:
            level = min(len(m.group(1)), 4)
            doc.add_heading(_MD_HEADING.sub("", stripped), level=level)
            continue
        if _MD_LIST.match(stripped):
            p = doc.add_paragraph(style="List Bullet")
            _write_runs(p, _MD_LIST.sub("", stripped))
            continue
        p = doc.add_paragraph()
        _write_runs(p, stripped)

    if footer:
        p = doc.add_paragraph()
        run = p.add_run(footer)
        run.italic = True
        run.font.size = None  # inherit a small caption style default

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
