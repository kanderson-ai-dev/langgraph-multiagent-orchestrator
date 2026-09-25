"""PDF export — renders the final report markdown as a downloadable PDF."""

import re

from fpdf import FPDF
from fpdf.enums import XPos, YPos

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#{1,6}\s*")
_MD_LIST = re.compile(r"^[-*]\s+")
_MAX_WORD = 80  # break pathological tokens (URLs, base64) FPDF can't fit


def _latin1(text: str) -> str:
    """FPDF core fonts are latin-1 — normalize anything else away."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _safe(text: str) -> str:
    text = _latin1(text)
    return " ".join(
        w
        if len(w) <= _MAX_WORD
        else "\n".join(w[i : i + _MAX_WORD] for i in range(0, len(w), _MAX_WORD))
        for w in text.split(" ")
    )


def _line(pdf: FPDF, text: str, *, h: float = 6.0) -> None:
    """One wrapped line returning to the left margin for the next."""
    pdf.multi_cell(0, h, _safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def markdown_to_pdf(markdown: str, *, title: str = "Report") -> bytes:
    """Convert markdown text to a simple PDF (headings, lists, body text)."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("helvetica", "B", 16)
    _line(pdf, title[:120], h=10)
    pdf.ln(4)

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            pdf.ln(3)
            continue
        if stripped.startswith("#"):
            pdf.set_font("helvetica", "B", 14)
            _line(pdf, _MD_HEADING.sub("", stripped), h=8)
            pdf.set_font("helvetica", "", 11)
            continue
        if _MD_LIST.match(stripped):
            pdf.set_font("helvetica", "", 11)
            _line(pdf, "  - " + _MD_BOLD.sub(r"\1", _MD_LIST.sub("", stripped)))
            continue
        pdf.set_font("helvetica", "", 11)
        _line(pdf, _MD_BOLD.sub(r"\1", stripped))
    return bytes(pdf.output())
