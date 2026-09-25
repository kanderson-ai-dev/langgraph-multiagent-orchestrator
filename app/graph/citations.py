"""Citation verification — a claim only counts if its quote is really there.

Three acceptance modes, cheapest first:
1. verbatim substring,
2. punctuation-insensitive substring,
3. sliding-window fuzzy similarity >= ``threshold`` (word-level).

Anything else is unsupported: dropped from the report and counted.
"""

import re
import string
from difflib import SequenceMatcher

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text.lower().translate(_PUNCT_TABLE)).strip()


def _window_similarity(quote_words: list[str], source_words: list[str]) -> float:
    """Best SequenceMatcher ratio of the quote against same-length windows."""
    n = len(quote_words)
    if n == 0 or len(source_words) < n:
        return 0.0
    quote = " ".join(quote_words)
    best = 0.0
    for i in range(len(source_words) - n + 1):
        window = " ".join(source_words[i : i + n])
        ratio = SequenceMatcher(None, quote, window).ratio()
        if ratio > best:
            best = ratio
    return best


def citation_supported(quote: str, source_text: str, *, threshold: float = 0.85) -> bool:
    """True if ``quote`` is verifiably present in ``source_text``."""
    if not quote or not source_text:
        return False
    if quote in source_text:  # 1. verbatim
        return True
    nq, ns = _normalize(quote), _normalize(source_text)
    if not nq or not ns:
        return False
    if nq in ns:  # 2. punctuation-insensitive
        return True
    # 3. sliding-window similarity
    return _window_similarity(nq.split(), ns.split()) >= threshold


def support_rate(supported: int, total: int) -> float:
    """Citation support rate; an empty draft has nothing verified."""
    return supported / total if total else 0.0
