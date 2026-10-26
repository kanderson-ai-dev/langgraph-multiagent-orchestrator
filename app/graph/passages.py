"""Passage index — ranked evidence windows for the Writer's prompt.

The Writer previously saw only ``text[:1500]`` of each source — often pure
boilerplate. Here every ``EvidenceItem`` is split into 1–3 sentence
passages (≈ 200–600 chars, stable ids ``S<i>-P<j>``), ranked against the
brief with a small BM25, then capped per-source and by a global character
budget so the prompt stays cheap.

Deterministic and dependency-free: the same evidence always produces the
same ids, which is what lets the extractive citation mode resolve a
``passage_id`` back to verbatim text without trusting the model to copy.
"""

import math
import re
from dataclasses import dataclass, field

from app.graph.state import EvidenceItem

_SENT_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u00c0-\u00de\"'(])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_PASSAGE_TARGET = 400
_PASSAGE_MAX = 700


@dataclass
class Passage:
    pid: str            # "S<i>-P<j>" — i = evidence index, j = passage index
    source_index: int
    url: str
    text: str
    score: float = 0.0


@dataclass
class PassageIndex:
    """Ranked passages plus id → passage lookup for citation resolution."""

    passages: list[Passage]
    by_id: dict[str, Passage] = field(default_factory=dict)

    def resolve(self, pid: str) -> Passage | None:
        return self.by_id.get(pid)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_END_RE.split(text) if s.strip()]


def _split_passages(text: str) -> list[str]:
    """Group sentences into ≈200–700-char passages (doc order preserved)."""
    passages: list[str] = []
    buf = ""
    for sent in _sentences(text):
        if buf and len(buf) + len(sent) + 1 > _PASSAGE_MAX and len(buf) >= 150:
            passages.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}" if buf else sent
    if buf:
        passages.append(buf)
    return passages


def _is_prose(passage: str) -> bool:
    """Real prose is mostly sentence-punctuated text, not a heading/bullet
    fragment with one sentence fused on.

    Titles, nav rows and listicle chrome must never become citable passages
    — a model that can only pick real sentences can't attribute a claim to
    a page heading. Requiring that ≥45% of the passage's characters sit
    inside proper sentences (≥30 chars, ending in .!?) keeps merged
    chrome-plus-sentence fragments out too.
    """
    if len(passage) < 120:
        return False
    covered = sum(
        len(m.group(0))
        for m in re.finditer(r"[A-Za-z][^.!?\n]{30,}[.!?]", passage)
    )
    return covered / len(passage) >= 0.45


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _bm25(passages: list[str], query: str) -> list[float]:
    """Tiny BM25 over the passage corpus — enough to beat 'first 1500 chars'."""
    docs = [_tokens(p) for p in passages]
    n = len(docs)
    if n == 0:
        return []
    df: dict[str, int] = {}
    avg = sum(len(d) for d in docs) / n or 1.0
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    q_terms = set(_tokens(query))
    k1, b = 1.2, 0.75
    scores: list[float] = []
    for d in docs:
        score = 0.0
        for t in q_terms:
            tf = d.count(t)
            if tf == 0:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        scores.append(score)
    return scores


def build_passage_index(
    evidence: list[EvidenceItem],
    query: str,
    *,
    per_source_k: int = 3,
    char_budget: int = 6000,
) -> PassageIndex:
    """Split each source into passages, rank globally, keep the budgeted top.

    Selection: top ``per_source_k`` per source (so no source disappears),
    ordered inside each source by score but rendered in document order.
    The global budget trims the lowest-scoring overflow.
    """
    # The evidence list may carry the same source several times (the
    # researcher runs once per supervisor round and state accumulates) —
    # index each unique URL once, else passages duplicate under different
    # ids and the prompt budget is spent on repeats.
    unique: list[EvidenceItem] = []
    seen_urls: set[str] = set()
    for e in evidence:
        if e.url not in seen_urls:
            seen_urls.add(e.url)
            unique.append(e)

    all_passages: list[Passage] = []
    flat_texts: list[str] = []
    for i, e in enumerate(unique):
        for j, ptext in enumerate(_split_passages(e.text)):
            if not _is_prose(ptext):
                continue  # headings/nav/list junk is never citable
            all_passages.append(
                Passage(pid=f"S{i}-P{j}", source_index=i, url=e.url, text=ptext)
            )
            flat_texts.append(ptext)
    for passage, score in zip(all_passages, _bm25(flat_texts, query), strict=True):
        passage.score = score

    # Top-K per source by score, rendered back in document order.
    kept: list[Passage] = []
    for i in range(len(unique)):
        mine = [p for p in all_passages if p.source_index == i]
        mine.sort(key=lambda p: -p.score)
        kept.extend(sorted(mine[:per_source_k], key=lambda p: int(p.pid.split("-P")[1])))

    # Global budget: drop the lowest-scoring until we fit.
    kept.sort(key=lambda p: p.score)
    total = sum(len(p.text) for p in kept)
    while kept and total > char_budget:
        total -= len(kept.pop(0).text)
    kept.sort(key=lambda p: (p.source_index, int(p.pid.split("-P")[1])))

    return PassageIndex(
        passages=kept,
        # Resolve against ALL source passages, not only the rendered top-K:
        # specialists and revision rounds may cite a valid id that the
        # budget trimmed from the prompt — it still maps to real evidence.
        by_id={p.pid: p for p in all_passages},
    )


def render_passages(index: PassageIndex) -> str:
    """The Writer's evidence block: sources with ranked, id-tagged passages."""
    if not index.passages:
        return "(no evidence gathered)"
    lines: list[str] = []
    last_src = -1
    for p in index.passages:
        if p.source_index != last_src:
            lines.append(f"\n[source {p.source_index}] {p.url}")
            last_src = p.source_index
        lines.append(f"  [{p.pid}] {p.text}")
    return "\n".join(lines).strip()
