"""PDF export — renders the final report markdown as a downloadable PDF.

Fonts: a bundled DejaVu Sans TTF family (see ``app/assets/fonts/``,
Bitstream Vera / Arev public license in ``LICENSE``) gives real Unicode
output — Latin/Latin-Extended, Greek, Cyrillic, Hebrew, Georgian,
Armenian and common symbols. Scripts outside DejaVu's coverage (notably
CJK, Thai, most Indic scripts) are replaced with a visible ``□``
placeholder plus a one-line notice in the exported report — honest
degradation instead of silent missing-glyph boxes (a bundled CJK font
would add ~15–20 MB to the image). When the font files are absent the
exporter degrades to FPDF's latin-1 core fonts with unrepresentable
characters replaced, exactly like before.

Markdown coverage mirrors the console renderer (``frontend/console/js/
util.js``): heading levels map to descending font sizes, unordered and
ordered lists keep their markers, ``**bold**``/``*italic*``/`` `code` ``
are unwrapped, and link targets stay visible — ``[label](url)`` items
and lines ending in a bare URL emit a blue underlined ``label — url``
row; inline links mid-paragraph fall back to ``text — url``.

Source URLs are deliberately rendered as *text*, never as PDF ``/URI``
link annotations: heuristic antivirus engines (e.g. McAfee's ML
reputation scanner) classify small, freshly-generated PDFs dense with
embedded external links as phishing lures and quarantine the download
(observed: ``Trojan:Script/Phishing.JD`` on every report). Each report
PDF has a unique hash, so reputation can never accumulate — annotations
stay out. PDF viewers (Chrome's PDFium, Acrobat) auto-linkify URLs in
the text layer, so the rows remain clickable in practice; the DOCX
export carries true hyperlinks.
"""

import re
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\w)(?:\*([^*\n]+)\*|_([^_\n]+)_)(?!\w)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADING = re.compile(r"^(#{1,6})\s*")
_MD_ULIST = re.compile(r"^[-*]\s+")
_MD_OLIST = re.compile(r"^(\d+)\.\s+")
_MD_HR = re.compile(r"^(-{3,}|\*{3,})$")
_MD_LINK_ITEM = re.compile(
    r"^((?:[-*]|\d+\.)\s+)?\[([^\]]+)\]\((https?://[^)\s]+)\)\s*$"
)
_MD_LINK_TAIL = re.compile(
    r"\[([^\]]+)\]\((https?://[^)\s]+)\)(\s+#\w+)?\s*$"
)
_MD_URL_TAIL = re.compile(r"(https?://\S+)(\s+#\w+)?\s*$")
_MAX_WORD = 80  # break pathological tokens (URLs, base64) FPDF can't fit

# Heading level → (font size, space before in mm). The printed report has
# no colour or weight contrast beyond bold, so size carries the hierarchy.
_HEADING_STYLE = {1: (15.0, 4.0), 2: (13.0, 3.0), 3: (12.0, 2.0)}

_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_FONT_FILES = {
    "": "DejaVuSans.ttf",
    "B": "DejaVuSans-Bold.ttf",
    "I": "DejaVuSans-Oblique.ttf",
}
# U+25A1 WHITE SQUARE — inside DejaVu's coverage; the latin-1 fallback
# path replaces with "?" natively.
_PLACEHOLDER = "□"

_cmap: frozenset[int] | None = None


def _dejavu_cmap() -> frozenset[int]:
    """Supported code points of the bundled family — read once per process."""
    global _cmap
    if _cmap is None:
        from fontTools.ttLib import TTFont  # type: ignore[import-untyped]

        font = TTFont(str(_FONT_DIR / _FONT_FILES[""]))
        _cmap = frozenset(font.getBestCmap())
    return _cmap


def _unsupported_chars(text: str, unicode_font: bool) -> frozenset[str]:
    """Characters the chosen output font cannot render."""
    if unicode_font:
        try:
            cmap = _dejavu_cmap()
        except Exception:  # fontTools missing/broken font → keep old behaviour
            return frozenset()
        return frozenset(
            ch for ch in text if ord(ch) > 0x7F and ord(ch) not in cmap
        )
    return frozenset(ch for ch in text if ord(ch) > 0xFF)


def _replace_unsupported(text: str, missing: frozenset[str]) -> str:
    if not missing:
        return text
    return "".join(_PLACEHOLDER if ch in missing else ch for ch in text)


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


def _plain(text: str) -> str:
    """Flatten inline markdown to printable text.

    Links keep both label and target; emphasis/code markers are unwrapped.
    """
    text = _MD_LINK.sub(r"\1 — \2", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return _MD_CODE.sub(r"\1", text)


def _url_row(
    pdf: FPDF,
    family: str,
    unicode_font: bool,
    url: str,
    tail: str,
    label: str = "",
    missing: frozenset[str] = frozenset(),
) -> None:
    """One row of link-looking text — blue, underlined, no annotation.

    The full URL is always printed (never truncated) so PDF viewers can
    auto-linkify it and readers can copy it; overlong URLs wrap instead.
    """
    indent = pdf.l_margin + 6
    pdf.set_x(indent)
    avail = pdf.w - pdf.r_margin - indent - 1
    pdf.set_font(family, "U", 9)
    pdf.set_text_color(5, 99, 193)
    text = f"{label} — {url}" if label else url
    text = (
        _replace_unsupported(text, missing)
        if unicode_font
        else _latin1(text)
    )
    if pdf.get_string_width(text) <= avail:
        pdf.cell(
            pdf.get_string_width(text) + 1, 5, text,
            new_x=XPos.RIGHT, new_y=YPos.TOP,
        )
        if tail:
            pdf.set_font(family, "", 9)
            pdf.set_text_color(110, 110, 110)
            tail = tail if unicode_font else _latin1(tail)
        pdf.cell(0, 5, tail, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        pdf.multi_cell(0, 5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if tail:
            pdf.set_font(family, "", 9)
            pdf.set_text_color(110, 110, 110)
            tail = tail if unicode_font else _latin1(tail)
            pdf.set_x(indent)
            pdf.multi_cell(0, 5, tail, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, "", 11)


def _safe(
    text: str, *, unicode_font: bool, missing: frozenset[str] = frozenset()
) -> str:
    text = _replace_unsupported(text, missing) if unicode_font else _latin1(text)
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
    pdf.set_title(title[:120] or "Report")
    pdf.set_author("LangGraph Multi-Agent Orchestrator")
    pdf.set_creator("LangGraph Multi-Agent Orchestrator")
    pdf.set_subject("Generated report export")
    pdf.set_producer("fpdf2")
    pdf.set_lang("en")
    pdf.add_page()
    missing = _unsupported_chars(title + markdown + footer, unicode_font)
    # latin-1 output already replaces with "?" natively; only the Unicode
    # path needs the explicit placeholder pass.
    visible = missing if unicode_font else frozenset()

    def line(text: str, *, h: float = 6.0) -> None:
        pdf.multi_cell(
            0, h, _safe(text, unicode_font=unicode_font, missing=visible),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    def text_line(text: str) -> None:
        """Body line honouring list markers, wrapped via multi_cell."""
        m = _MD_ULIST.match(text)
        if m:
            line("  - " + _plain(_MD_ULIST.sub("", text)))
            return
        m = _MD_OLIST.match(text)
        if m:
            line(f"  {m.group(1)}. {_plain(_MD_OLIST.sub('', text))}")
            return
        line(_plain(text))

    pdf.set_font(family, "B", 16)
    line(title[:120], h=10)
    pdf.ln(4)
    if missing:
        pdf.set_font(family, "I", 9)
        pdf.set_text_color(110, 110, 110)
        placeholder = _PLACEHOLDER if unicode_font else "?"
        line(
            f"Note: {len(missing)} unsupported character(s) were replaced "
            f"with {placeholder} (font coverage limit).",
            h=5.0,
        )
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(family, "", 11)

    for raw in markdown.splitlines():
        stripped = raw.strip()
        if not stripped:
            pdf.ln(3)
            continue
        if _MD_HR.match(stripped):
            pdf.ln(2)
            continue
        m = _MD_HEADING.match(stripped)
        if m:
            size, gap = _HEADING_STYLE.get(len(m.group(1)), (11.0, 2.0))
            pdf.ln(gap)
            pdf.set_font(family, "B", size)
            line(_plain(_MD_HEADING.sub("", stripped)), h=7.5)
            pdf.set_font(family, "", 11)
            continue
        m = _MD_LINK_ITEM.match(stripped)
        if m:  # "- [label](url)" → label line + visible URL row
            marker = (m.group(1) or "").strip()
            line((f"  {marker} " if marker else "") + _plain(m.group(2)))
            _url_row(pdf, family, unicode_font, m.group(3), "", missing=visible)
            continue
        m = _MD_LINK_TAIL.search(stripped)
        if m and m.start(0) > 0:  # head text … [label](url) at end of line
            head = stripped[: m.start(0)].rstrip()
            text_line(head)
            _url_row(
                pdf, family, unicode_font, m.group(2),
                (m.group(3) or "").strip(), label=_plain(m.group(1)),
                missing=visible,
            )
            continue
        m = _MD_URL_TAIL.search(stripped)
        if m and (m.start(1) == 0 or stripped[m.start(1) - 1] != "("):
            head = stripped[: m.start(1)].rstrip()
            url = m.group(1).rstrip(").,;]>\"'")
            if head:
                text_line(head)
            _url_row(
                pdf, family, unicode_font, url, (m.group(2) or "").strip(),
                missing=visible,
            )
            continue
        text_line(stripped)
    if footer:
        pdf.ln(6)
        pdf.set_font(family, "I", 8)
        line(_plain(footer), h=4.0)
    return bytes(pdf.output())
