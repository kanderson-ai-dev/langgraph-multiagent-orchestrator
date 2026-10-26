"""Citation verification — a claim only counts if its quote is really there.

Three acceptance modes, cheapest first:
1. verbatim substring,
2. punctuation-insensitive substring,
3. sliding-window fuzzy similarity >= ``threshold`` (word-level) followed
   by a **critical-token check**: the digits/units/negations inside the
   quote must exactly match those inside the best window, and every
   capitalized entity in the quote must appear there. A quote that swaps
   "12%" for "21%", drops a "not", or renames an entity is unsupported no
   matter how high the fuzzy ratio scores.

Anything else is unsupported: dropped from the report and counted.
"""

import re
import string
import unicodedata
from difflib import SequenceMatcher

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_WS_RE = re.compile(r"\s+")

# Tokens that flip a claim's meaning when added, dropped, or swapped.
# Normalized form: "didn't" → "didnt" (punctuation already stripped).
_NEGATIONS = frozenset(
    {
        "not", "no", "never", "none", "nor", "neither", "cannot",
        "without", "didnt", "doesnt", "dont", "isnt", "wasnt", "werent",
        "cant", "couldnt", "wont", "wouldnt", "shouldnt", "hasnt",
        "havent", "hadnt", "aint",
    }
)

# Unit vocabulary — only critical when directly following a number.
_UNITS = frozenset(
    {
        "b", "kb", "mb", "gb", "tb", "pb", "kib", "mib", "gib",
        "g", "kg", "mg", "t",
        "m", "km", "cm", "mm",
        "s", "ms", "ns", "hz", "khz", "mhz", "ghz",
        "usd", "eur", "gbp", "jpy", "aud", "cad", "chf", "cny",
        "dollar", "dollars", "euro", "euros", "pound", "pounds",
        "thousand", "million", "billion", "trillion",
        "percent", "percentage", "bps",
    }
)

_CURRENCY_SYMBOLS = frozenset("$€£¥₹")

_CAP_RE = re.compile(r"[A-Z][\w.-]*")

# Quote-side only: Markdown decoration is the writer's syntax, not source
# text — strip it before any comparison.
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# An ellipsis the writer placed to span a gap: "…" (any run) or "...".
_ELLIPSIS_RE = re.compile(r"…+|(?:\.\s*){2,}")


def _strip_markdown(quote: str) -> str:
    return _MD_LINK_RE.sub(r"\1", quote).replace("`", "")


def _normalize(text: str) -> str:
    """Case-fold + punctuation/whitespace-insensitive normal form.

    ``NFKC`` folds curly quotes to ASCII, full-width forms to ASCII,
    ellipses to ``...`` and no-break spaces to plain spaces; a Unicode
    punctuation-category sweep then removes what NFKC leaves (em/en
    dashes, guillemets) the same way ``_PUNCT_TABLE`` removes ASCII
    punctuation, and format characters (soft hyphens, zero-width spaces)
    are dropped entirely. Applied identically to quote and source, so a
    scraped page's typography can no longer fail a genuinely verbatim
    quote.
    """
    folded = unicodedata.normalize("NFKC", text)
    stripped = folded.translate(_PUNCT_TABLE)
    cleaned = "".join(
        "" if unicodedata.category(ch) == "Cf"
        else " " if unicodedata.category(ch).startswith("P")
        else ch
        for ch in stripped
    )
    return _WS_RE.sub(" ", cleaned.lower()).strip()


def _spans_supported(quote: str, source_text: str) -> bool | None:
    """Ellipsis rule — ``None`` when the quote carries no ellipsis.

    A quote like ``"growth of 40 % … across all pilot sites"`` marks a
    span across source text. Every segment must normalize to ≥ 4 words
    (shorter fragments are unverifiable) and appear **in order** in the
    normalized source — an exact, case/punctuation-insensitive substring,
    so digits, entities and negations inside each segment still have to
    match literally.
    """
    if not _ELLIPSIS_RE.search(quote):
        return None
    segments = [
        _normalize(_strip_markdown(p)) for p in _ELLIPSIS_RE.split(quote)
    ]
    ns = _normalize(source_text)
    pos = 0
    for seg in segments:
        if not seg:
            continue
        if len(seg.split()) < 4:
            return False
        found = ns.find(seg, pos)
        if found < 0:
            return False
        pos = found + len(seg)
    return True


def _best_window(
    quote_words: list[str], source_words: list[str]
) -> tuple[float, list[str]]:
    """(ratio, window) — best SequenceMatcher score and its token list."""
    n = len(quote_words)
    if n == 0 or len(source_words) < n:
        return 0.0, []
    quote = " ".join(quote_words)
    best, best_window = 0.0, source_words[:n]
    for i in range(len(source_words) - n + 1):
        window = source_words[i : i + n]
        ratio = SequenceMatcher(None, quote, " ".join(window)).ratio()
        if ratio > best:
            best, best_window = ratio, window
    return best, best_window


def _critical(tokens: list[str]) -> frozenset[str]:
    """Case-insensitive critical tokens: anything containing a digit, a
    unit directly following a number, and negation terms."""
    crit: set[str] = set()
    for i, tok in enumerate(tokens):
        if any(ch.isdigit() for ch in tok):
            crit.add(tok)
            if (
                i + 1 < len(tokens)
                and tokens[i + 1].isalpha()
                and tokens[i + 1] in _UNITS
            ):
                crit.add(tokens[i + 1])
        elif tok in _NEGATIONS:
            crit.add(tok)
    return frozenset(crit)


def _entities(quote: str) -> frozenset[str]:
    """Capitalized entities from the *raw* quote (case dies in
    normalization). The leading token is skipped when it is a plain
    capitalized word — sentence-initial case is not an entity signal;
    all-caps acronyms still count wherever they sit."""
    crit: set[str] = set()
    for m in _CAP_RE.finditer(quote):
        word = m.group(0)
        if m.start() == 0 and not word.isupper():
            continue
        ent = _normalize(word)
        if ent and len(ent) > 1:
            crit.add(ent)
    return frozenset(crit)


def citation_supported(quote: str, source_text: str, *, threshold: float = 0.85) -> bool:
    """True if ``quote`` is verifiably present in ``source_text``."""
    if not quote or not source_text:
        return False
    if quote in source_text:  # 1. verbatim
        return True
    quote = _strip_markdown(quote)
    if quote in source_text:
        return True
    spans = _spans_supported(quote, source_text)
    if spans is not None:
        return spans
    nq, ns = _normalize(quote), _normalize(source_text)
    if not nq or not ns:
        return False
    if nq in ns:  # 2. punctuation-insensitive
        return True
    # 3. sliding-window similarity, then the critical-token check on the
    # best window — the ratio alone cannot see a swapped "12%"→"21%".
    ratio, window = _best_window(nq.split(), ns.split())
    if ratio < threshold:
        return False
    # Extractable classes must match *exactly* — bidirectional, so a
    # dropped "not" or a dropped number fails just like a changed one.
    if _critical(nq.split()) != _critical(window):
        return False
    # Entities only check quote → window (the window lost its case when
    # normalized); currency symbols compare on the raw source.
    if not _entities(quote).issubset(set(window)):
        return False
    return not (
        set(quote) & _CURRENCY_SYMBOLS
    ) - (set(source_text) & _CURRENCY_SYMBOLS)


def support_rate(supported: int, total: int) -> float:
    """Citation support rate; an empty draft has nothing verified."""
    return supported / total if total else 0.0


def explain_quote(
    quote: str, source_text: str, *, threshold: float = 0.85
) -> dict[str, object]:
    """Instrumented ``citation_supported`` — same checks, plus *why*.

    Returns ``{"supported": bool, "stage": <first failing stage>,
    "best_ratio": float}`` where stage is one of ``verbatim``,
    ``punctuation``, ``fuzzy`` (ratio below threshold), ``critical``,
    ``entities``, ``currency`` or ``ok``. Used by the live-gate diagnosis
    tool to turn a bare dropped-count into a failure taxonomy.
    """
    fail = {"supported": False, "best_ratio": 0.0}
    if not quote or not source_text:
        return {**fail, "stage": "empty"}
    if quote in source_text:
        return {"supported": True, "stage": "ok", "best_ratio": 1.0}
    quote = _strip_markdown(quote)
    if quote in source_text:
        return {"supported": True, "stage": "ok", "best_ratio": 1.0}
    spans = _spans_supported(quote, source_text)
    if spans is not None:
        return {
            "supported": spans,
            "stage": "ok" if spans else "spans",
            "best_ratio": 1.0 if spans else 0.0,
        }
    nq, ns = _normalize(quote), _normalize(source_text)
    if not nq or not ns:
        return {**fail, "stage": "empty"}
    if nq in ns:
        return {"supported": True, "stage": "ok", "best_ratio": 1.0}
    ratio, window = _best_window(nq.split(), ns.split())
    if ratio < threshold:
        return {**fail, "stage": "fuzzy", "best_ratio": ratio}
    if _critical(nq.split()) != _critical(window):
        return {**fail, "stage": "critical", "best_ratio": ratio}
    if not _entities(quote).issubset(set(window)):
        return {**fail, "stage": "entities", "best_ratio": ratio}
    if (set(quote) & _CURRENCY_SYMBOLS) - (set(source_text) & _CURRENCY_SYMBOLS):
        return {**fail, "stage": "currency", "best_ratio": ratio}
    return {"supported": True, "stage": "ok", "best_ratio": ratio}
