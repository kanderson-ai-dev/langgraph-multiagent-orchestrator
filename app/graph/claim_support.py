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

import re
from typing import Any, Literal

from pydantic import BaseModel

from app.graph.state import Citation


class ClaimVerdicts(BaseModel):
    """One verdict per citation, same order as given."""

    verdicts: list[Literal["yes", "partly", "no"]]


_SYSTEM = (
    "You are a fact-checking judge. For each numbered item you are given a "
    "claim from a report and the exact quote that was cited to support it. "
    "First classify the claim: FACTUAL claims assert something a source "
    "could literally state (names, numbers, events, capabilities); "
    "ANALYTIC claims are the report author's own synthesis — "
    "recommendations, judgments, outlook, implications. Then decide "
    "whether the quote genuinely supports the claim: for FACTUAL claims, "
    "'yes' = the quote directly states or clearly implies the claim; for "
    "ANALYTIC claims, 'yes' = the quote provides the factual basis for "
    "that judgment (a cost recommendation citing cost evidence counts; a "
    "recommendation citing merely topical text does not). In both cases "
    "'partly' = thin, partial, or heavily-interpreted support and "
    "'no' = unrelated or contradicting. Answer with one verdict per item, "
    "in order, nothing else."
)


_WS_RE = re.compile(r"[^a-z0-9]+")


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
    auto_yes = [_verbatim_supported(c.claim, c.quote) for c in citations]
    to_judge = [(i, c) for i, c in enumerate(citations) if not auto_yes[i]]
    if not to_judge:
        return ["yes"] * len(citations), 1.0

    from app.core.cost_tracking import tracked_structured

    items = "\n\n".join(
        f"[{k}] claim: {c.claim}\nquote: {c.quote}\nsource: {c.source_url}"
        for k, (_, c) in enumerate(to_judge)
    )
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
    judged = [str(v) for v in getattr(out, "verdicts", [])]
    if not judged:
        # Stub/no-output path — adjudication is a documented no-op.
        return [], 0.0
    if len(judged) < len(to_judge):
        # A real judge that short-lists leaves some claims unadjudicated;
        # an unadjudicated claim is not a supported one — pad as "no".
        judged += ["no"] * (len(to_judge) - len(judged))
    verdicts = ["yes" if y else "no" for y in auto_yes]
    for (i, _), v in zip(to_judge, judged[: len(to_judge)], strict=False):
        verdicts[i] = v
    return verdicts, claim_support_rate(verdicts)
