"""Claim-support integrity check — the "does the quote back the claim?" metric.

``citation_support`` proves the *quote exists* in the cited source. It does
not prove the quote actually supports the *claim* — extractive citations
make existence trivially true, so an independent check is what keeps the
metric honest (this is the upgrade plan's integrity guard, D3).

Eval-time only, never in the request path. One batched call per case: the
judge model (a different model than the Writer) labels every citation
``yes`` / ``partly`` / ``no``; ``claim_support_rate`` is the share of
``yes``. Verdicts are stored per-citation in the live run file so they can
be hand-checked for calibration.
"""

from typing import Any, Literal

from pydantic import BaseModel

from app.graph.state import Citation


class ClaimVerdicts(BaseModel):
    """One verdict per citation, same order as given."""

    verdicts: list[Literal["yes", "partly", "no"]]


_SYSTEM = (
    "You are a fact-checking judge. For each numbered item you are given a "
    "claim from a report and the exact quote that was cited to support it. "
    "Decide whether the quote genuinely supports the claim: 'yes' = the "
    "quote directly states or clearly implies the claim; 'partly' = it "
    "supports only part of it or requires heavy interpretation; 'no' = "
    "unrelated or contradicting. Answer with one verdict per item, in "
    "order, nothing else."
)


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
    from app.core.cost_tracking import tracked_structured

    items = "\n\n".join(
        f"[{i}] claim: {c.claim}\nquote: {c.quote}\nsource: {c.source_url}"
        for i, c in enumerate(citations)
    )
    out = await tracked_structured(
        judge_llm,
        ClaimVerdicts,
        role="claim_judge",
        system=_SYSTEM,
        user=items,
        context={"prompt_name": "claim_support"},
    )
    verdicts = [str(v) for v in getattr(out, "verdicts", [])]
    return verdicts, claim_support_rate(verdicts)
