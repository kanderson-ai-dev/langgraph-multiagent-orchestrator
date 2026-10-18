"""PDF export — renders the final report markdown as a downloadable PDF.

Fonts: a bundled DejaVu Sans TTF family (see ``app/assets/fonts/``,
Bitstream Vera / Arev public license in ``LICENSE``) gives real Unicode
output — Latin/Latin-Extended, Greek, Cyrillic, Hebrew, Georgian,
Armenian and common symbols. Scripts outside DejaVu's coverage (notably
CJK, Thai, most Indic scripts) render as missing-glyph boxes rather than
crashing — documented in README "Known Limitations". When the font files
are absent the exporter degrades to FPDF's latin-1 core fonts with
unrepresentable characters replaced, exactly like before.
"""

import re
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#{1,6}\s*")
_MD_LIST = re.compile(r"^[-*]\s+")
_MAX_WORD = 80  # break pathological tokens (URLs, base64) FPDF can't fit

_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_FONT_FILES = {
    "": "DejaVuSans.ttf",
    "B": "DejaVuSans-Bold.ttf",
    "I": "DejaVuSans-Oblique.ttf",
}


def _font_family(pdf: FPDF) -> str:
    """Register the bundled Unicode family; fall back to latin-1 core."""
    if all((_FONT_DIR / f).exists() for f in _FONT_FILES.values()):
        for style, filename in _FONT_FILES.items():
            pdf.add_font("DejaVu", style, str(_FONT_DIR / filename))
        return "DejaVu"
    return "helvetica"


def _latin1(text: str) -> str:
    """FPDF core fonts are latin-1 — normalize anything else away."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _safe(text: str, *, unicode_font: bool) -> str:
    if not unicode_font:
        text = _latin1(text)
    return " ".join(
        w
        if len(w) <= _MAX_WORD
        else "\n".join(w[i : i + _MAX_WORD] for i in range(0, len(w), _MAX_WORD))
        for w in text.split(" ")
    )


def markdown_to_pdf(
    markdown: str, *, title: str = "Report", footer: str = ""
) -> bytes:
    """Convert markdown text to a simple PDF (headings, lists, body text).

    ``footer`` is rendered in small italics at the end of the document —
    used for prompt-version provenance.
    """
    pdf = FPDF()
    family = _font_family(pdf)
    unicode_font = family == "DejaVu"
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def line(text: str, *, h: float = 6.0) -> None:
        pdf.multi_cell(
            0, h, _safe(text, unicode_font=unicode_font),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    pdf.set_font(family, "B", 16)
    line(title[:120], h=10)
    pdf.ln(4)

    for raw in markdown.splitlines():
        stripped = raw.strip()
        if not stripped:
            pdf.ln(3)
            continue
        if stripped.startswith("#"):
            pdf.set_font(family, "B", 14)
            line(_MD_HEADING.sub("", stripped), h=8)
            pdf.set_font(family, "", 11)
            continue
        if _MD_LIST.match(stripped):
            pdf.set_font(family, "", 11)
            line("  - " + _MD_BOLD.sub(r"\1", _MD_LIST.sub("", stripped)))
            continue
        pdf.set_font(family, "", 11)
        line(_MD_BOLD.sub(r"\1", stripped))
    if footer:
        pdf.ln(6)
        pdf.set_font(family, "I", 8)
        line(footer, h=4.0)
    return bytes(pdf.output())
