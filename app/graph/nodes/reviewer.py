"""Reviewer/Auditor worker — grades the draft and verifies every citation.

Two-stage audit:
1. mechanical: each citation's quote must be verifiably present in the
   fetched source text (``citation_supported``) — unsupported quotes are
   dropped and counted;
2. judgmental: the LLM scores the rubric and issues ``approve``/``revise``.

A support rate below threshold forces ``revise`` — quality is measured,
never assumed.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.framework.worker import StateUpdate
from app.graph.citations import citation_supported, support_rate
from app.graph.prompts import REVIEWER_SYSTEM
from app.graph.state import (
    AgentMessage,
    Brief,
    Citation,
    EvidenceItem,
    ReportDraft,
    ReviewVerdict,
    Verdict,
)
from app.services.llm_client import StructuredLLM

_SUPPORT_THRESHOLD = 0.90  # matches the EDD gate in the project objective


class ReviewOutput(BaseModel):
    """LLM-produced audit of the current draft."""

    verdict: str = Field(description="approve | revise")
    score: float = Field(ge=1.0, le=5.0)
    rubric: dict[str, float] = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list)


def verify_citations(
    draft: ReportDraft, evidence: list[EvidenceItem]
) -> tuple[list[Citation], int]:
    """Split citations into (supported, unsupported-count) against evidence."""
    by_url = {e.url: e.text for e in evidence}
    kept: list[Citation] = []
    dropped = 0
    for c in draft.citations:
        text = by_url.get(c.source_url, "")
        if not text:  # URL unknown — check across all evidence as fallback
            text = "\n".join(e.text for e in evidence)
        if citation_supported(c.quote, text):
            kept.append(c)
        else:
            dropped += 1
    return kept, dropped


class ReviewerWorker:
    name = "reviewer"
    description = "Audits drafts against the rubric and verifies citations"

    def __init__(self, llm: StructuredLLM) -> None:
        self._llm = llm

    def _prompt(self, brief: Brief, draft: ReportDraft, kept: list[Citation]) -> str:
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"
        cites = "\n".join(
            f"- [{c.source_url}] {c.claim} (quote: {c.quote[:120]}…)" for c in kept
        ) or "(no supported citations)"
        return (
            f"Brief — topic: {brief.topic}; audience: {brief.audience}; "
            f"tone: {brief.tone}\nRequirements:\n{reqs}\n\n"
            f"Draft v{draft.version} title: {draft.title}\n\n"
            f"{draft.markdown}\n\nSupported citations:\n{cites}"
        )

    async def run(self, state: dict[str, Any]) -> StateUpdate:
        brief: Brief = state["brief"]
        drafts: list[ReportDraft] = state.get("drafts", [])
        evidence: list[EvidenceItem] = state.get("evidence", [])
        debate_round = state.get("debate_round", 0) + 1

        if not drafts:
            verdict = ReviewVerdict(
                draft_version=0,
                verdict="revise",
                score=1.0,
                feedback=["No draft exists to review."],
            )
            return {
                "latest_verdict": verdict,
                "debate_round": debate_round,
                "transcript": [
                    AgentMessage(
                        sender=self.name, recipient="supervisor",
                        kind="verdict", content="No draft to review.",
                        round=debate_round,
                    )
                ],
            }

        draft = drafts[-1]
        kept, dropped = verify_citations(draft, evidence)

        out = await self._llm.structured(
            ReviewOutput,
            system=REVIEWER_SYSTEM,
            user=self._prompt(brief, draft, kept),
            context={"draft_version": draft.version},
        )

        rate = support_rate(len(kept), len(draft.citations))
        verdict_str: Verdict = "approve" if out.verdict == "approve" else "revise"
        feedback = list(out.feedback)
        if dropped:
            feedback.append(
                f"{dropped} citation(s) could not be verified against the fetched "
                f"source text (support rate {rate:.2f}); re-cite or remove them."
            )
            if rate < _SUPPORT_THRESHOLD:
                verdict_str = "revise"  # measured quality gate, not vibes

        verdict = ReviewVerdict(
            draft_version=draft.version,
            verdict=verdict_str,
            score=min(max(out.score, 1.0), 5.0),
            rubric=out.rubric,
            feedback=feedback,
            unsupported_citations=dropped,
        )

        msg = AgentMessage(
            sender=self.name,
            recipient="writer" if verdict_str == "revise" else "supervisor",
            kind="verdict",
            content=(
                f"v{draft.version}: {verdict_str} (score {verdict.score:.1f}, "
                f"support {rate:.2f})"
            ),
            round=debate_round,
        )
        return {
            "latest_verdict": verdict,
            "debate_round": debate_round,
            "transcript": [msg],
        }
