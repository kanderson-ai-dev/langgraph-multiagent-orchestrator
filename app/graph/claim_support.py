"""Claim-support integrity check — the "does the quote back the claim?" metric.

``citation_support`` proves the *quote exists* in the cited source. It does
not prove the quote actually supports the *claim* — extractive citations
make existence trivially true, so semantic adjudication is what keeps the
metric honest (the upgrade plan's integrity guard, D3).

Two call sites share this module: the Reviewer runs it inside the debate
loop on the *quote-verified* citations so weak claims can be revised or
dropped before approval, and the live gate re-runs it post-hoc with an
independent judge model (a different model than the Writer). Each call
labels every citation ``yes`` / ``partly`` / ``no``;
``claim_support_rate`` is the share of ``yes``. Verdicts are stored
per-citation in the live run file so they can be hand-checked for
calibration.
"""

import contextlib
import re
from typing import Any, Literal

from pydantic import BaseModel

from app.graph.state import Citation


class ClaimVerdicts(BaseModel):
    """One verdict per citation, same order as given."""

    verdicts: list[Literal["yes", "partly", "no"]]


_SYSTEM = (
    "You are a strict fact-checking judge. For each numbered item you are "
    "given a claim from a report and the exact quote that was cited to "
    "support it. Decide whether the quote supports the claim:\n"
    "'yes' = the quote directly states the claim's core assertion, or the "
    "claim is a near-verbatim restatement or light paraphrase of the "
    "quote. Numbers, names and dates in the claim must appear in — or be "
    "trivially entailed by — the quote. A mild evaluative tail ('is "
    "crucial', 'suggests potential', 'which may not reflect the full "
    "picture') does not downgrade an otherwise stated core — but a tail "
    "asserting a NEW unsupported fact does.\n"
    "'partly' = the quote is on-topic and supports part of the claim, but "
    "a material part of the claim's assertion is absent, or the claim "
    "adds inference or specificity the quote does not state. A compound "
    "claim where only one part is supported is 'partly'.\n"
    "'no' = the quote is unrelated, site boilerplate/navigation, "
    "contradicts the claim, or the claim's operative figures, entities "
    "or dates are absent from it. Topical relevance alone is NOT "
    "support.\n"
    "Recommendations and judgments are the report author's own voice: "
    "'yes' only when the quote states the factual premise the claim "
    "rests on; 'partly' when it merely shares the topic.\n"
    "Answer with one verdict per item, in order, nothing else."
)


_WS_RE = re.compile(r"[^a-z0-9]+")
# Digit runs, optionally with thousands separators or decimals. Single
# digits are ignored — they are enumerators/version tails ("SOC 2") more
# often than statistics, and flagging them would cry wolf.
_FIG_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _figure_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for tok in _FIG_RE.findall(text):
        t = tok.rstrip(".").replace(",", "")
        if len(t) < 2:
            continue
        out.add(t)
        with contextlib.suppress(ValueError):
            out.add(f"{float(t):g}")  # "14.10" -> "14.1", "7500.0" -> "7500"
    return out


def missing_claim_figures(claim: str, quote: str) -> list[str]:
    """Numbers the claim asserts that appear nowhere in its quote.

    A figure absent from the cited passage cannot be supported by it —
    "the quote is on-topic" does not make a fabricated statistic true.
    """
    return sorted(_figure_tokens(claim) - _figure_tokens(quote))


def _verbatim_supported(claim: str, quote: str) -> bool:
    """A claim that is a verbatim substring of the quote is ``yes`` by
    definition — the source directly states it; no judge call needed.

    Normalization collapses punctuation/whitespace; the spaceless pass
    rescues quotes whose whitespace PDF extraction ate ("lost over
    $200 million" vs ``lostover200million``).
    """
    cn = _WS_RE.sub(" ", claim.lower()).strip()
    qn = _WS_RE.sub(" ", quote.lower())
    if len(cn.split()) >= 6 and f" {cn} " in f" {qn} ":
        return True
    squash_c = cn.replace(" ", "")
    squash_q = qn.replace(" ", "")
    return len(squash_c) >= 40 and squash_c in squash_q


def claim_support_rate(verdicts: list[str]) -> float:
    return sum(1 for v in verdicts if v == "yes") / len(verdicts) if verdicts else 0.0


async def judge_claim_support(
    citations: list[Citation], *, judge_llm: Any
) -> tuple[list[str], float]:
    """Batch-judge one case's citations; returns (verdicts, rate).

    ``StubLLM`` callers get ``model_construct`` output — override by
    registering a ``ClaimVerdicts`` builder in tests.
    """
    if not citations:
        return [], 0.0
    # Deterministic passes first — no judge ballot needed when the
    # outcome is decidable in code: a verbatim claim is 'yes' by
    # definition, and a claim citing figures absent from its own quote
    # can never be fully supported, so it is 'partly' by construction.
    auto: list[str | None] = []
    for c in citations:
        if _verbatim_supported(c.claim, c.quote):
            auto.append("yes")
        elif missing_claim_figures(c.claim, c.quote):
            auto.append("partly")
        else:
            auto.append(None)
    to_judge = [(i, c) for i, c in enumerate(citations) if auto[i] is None]
    if not to_judge:
        verdicts = [a if a is not None else "yes" for a in auto]
        return verdicts, claim_support_rate(verdicts)

    from app.core.cost_tracking import tracked_structured

    items = "\n\n".join(
        f"[{k}] claim: {c.claim}\nquote: {c.quote}\nsource: {c.source_url}"
        for k, (_, c) in enumerate(to_judge)
    )
    # Three samples per batch, majority vote — hand-labelling showed a
    # single judge sample drifts ~20 % on boundary items (verbatim-with-
    # tail downgraded to ``partly``, occasional clear misses); majority
    # voting removes the variance without loosening the standard.
    ballots: list[list[str]] = []
    for _ in range(3):
        out = await tracked_structured(
            judge_llm,
            ClaimVerdicts,
            role="claim_judge",
            system=_SYSTEM,
            user=(
                f"{len(to_judge)} items follow — return exactly "
                f"{len(to_judge)} verdicts, in order.\n\n{items}"
            ),
            context={"prompt_name": "claim_support"},
        )
        ballots.append([str(v) for v in getattr(out, "verdicts", [])])
    if not any(ballots):
        # Stub/no-output path — adjudication is a documented no-op.
        return [], 0.0
    judged = _majority(ballots, len(to_judge))
    verdicts = [a if a is not None else "no" for a in auto]
    for (i, _), v in zip(to_judge, judged, strict=False):
        verdicts[i] = v
    return verdicts, claim_support_rate(verdicts)


def _majority(ballots: list[list[str]], n: int) -> list[str]:
    """Per-item majority vote across judge samples; unadjudicated or
    tied items resolve to ``no`` (an unadjudicated claim is not a
    supported one)."""
    from collections import Counter

    out: list[str] = []
    for k in range(n):
        votes = Counter(b[k] for b in ballots if k < len(b))
        top = votes.most_common()
        out.append(top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else "no")
    return out
