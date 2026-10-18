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

from app.core.metrics import DEBATE_ROUNDS_TOTAL

# Same constant the EDD gate enforces — single source in app.core.thresholds.
from app.core.thresholds import EDD_THRESHOLDS
from app.framework.worker import StateUpdate
from app.graph.citations import citation_supported, support_rate
from app.graph.state import (
    AgentMessage,
    Brief,
    Citation,
    EvidenceItem,
    ReportDraft,
    ReviewVerdict,
    Verdict,
)
from app.graph.teams import SECTION_TEMPLATES
from app.services.llm_client import StructuredLLM
from app.services.prompt_registry import PromptRegistry

_SUPPORT_THRESHOLD = EDD_THRESHOLDS["citation_support_rate"]


class Rubric(BaseModel):
    """Fixed-shape rubric — free-form ``dict`` fields are not valid in
    OpenAI strict structured outputs (``additionalProperties`` is banned)."""

    structure: float = Field(ge=1.0, le=5.0)
    clarity: float = Field(ge=1.0, le=5.0)
    grounding: float = Field(ge=1.0, le=5.0)
    tone: float = Field(ge=1.0, le=5.0)


class ReviewOutput(BaseModel):
    """LLM-produced audit of the current draft (strict-output safe)."""

    verdict: str = Field(description="approve | revise")
    score: float = Field(ge=1.0, le=5.0)
    rubric: Rubric
    feedback: list[str]


def verify_citations(
    draft: ReportDraft, evidence: list[EvidenceItem]
) -> tuple[list[Citation], int, list[str]]:
    """Split citations into (supported, dropped-count, source-hash) tuples.

    ``kept`` and ``source_hashes`` are aligned: each supported citation
    records the ``sha256`` of the evidence item that verified it (empty
    string for pre-seal legacy items).

    A citation verifies only against the source it *cites* — never against
    pooled evidence text. A quote that exists in source B but is credited
    to source A (or to a phantom URL) is contamination, not support.
    """
    by_url = {e.url: e for e in evidence}
    kept: list[Citation] = []
    source_hashes: list[str] = []
    dropped = 0
    for c in draft.citations:
        item = by_url.get(c.source_url)
        if item is not None and citation_supported(c.quote, item.text):
            kept.append(c)
            source_hashes.append(item.sha256)
        else:
            dropped += 1
    return kept, dropped, source_hashes


class ReviewerWorker:
    name = "reviewer"
    description = "Audits drafts against the rubric and verifies citations"

    def __init__(
        self,
        llm: StructuredLLM,
        prompts: PromptRegistry,
        *,
        citation_shortcircuit: bool = True,
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._citation_shortcircuit = citation_shortcircuit

    def _prompt(self, brief: Brief, draft: ReportDraft, kept: list[Citation]) -> str:
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"
        cites = "\n".join(
            f"- [{c.source_url}] {c.claim} (quote: {c.quote[:120]}…)" for c in kept
        ) or "(no supported citations)"
        # Grade structure against the contract the Writer was given — the
        # reviewer previously invented its own outline and demanded sections
        # (e.g. "Findings") that don't exist in this report type's template.
        template = SECTION_TEMPLATES.get(
            brief.report_type, SECTION_TEMPLATES["general"]
        )
        return (
            f"Brief — topic: {brief.topic}; type: {brief.report_type}; "
            f"audience: {brief.audience}; tone: {brief.tone}\n"
            + (f"Client context: {brief.tenant_context}\n" if brief.tenant_context else "")
            + f"Requirements:\n{reqs}\n"
            f"Expected section structure: {template}\n\n"
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
        # Deterministic citation pre-check runs before the LLM call — a
        # draft that already fails verification will be revised anyway, so
        # spending tokens on the rubric is pure waste.
        kept, dropped, _hashes = verify_citations(draft, evidence)
        DEBATE_ROUNDS_TOTAL.inc()
        rate = support_rate(len(kept), len(draft.citations))

        if (
            self._citation_shortcircuit
            and dropped
            and rate < _SUPPORT_THRESHOLD
        ):
            feedback = [
                f"{dropped} citation(s) could not be verified against the "
                f"fetched source text (support rate {rate:.2f}); re-cite or "
                "remove them."
            ]
            verdict = ReviewVerdict(
                draft_version=draft.version,
                verdict="revise",
                score=3.0,
                rubric={},
                feedback=feedback,
                unsupported_citations=dropped,
            )
            msg = AgentMessage(
                sender=self.name, recipient="writer", kind="verdict",
                content=(
                    f"v{draft.version}: revise (citation pre-check failed, "
                    f"support {rate:.2f})"
                ),
                round=debate_round,
            )
            from app.core.cost_tracking import current_cost

            return {
                "latest_verdict": verdict,
                "debate_round": debate_round,
                "cost_so_far": current_cost(),
                "transcript": [msg],
            }

        from app.core.cost_tracking import tracked_structured

        system = self._prompts.render("reviewer")
        out = await tracked_structured(
            self._llm,
            ReviewOutput,
            role=self.name,
            system=system.text,
            user=self._prompt(brief, draft, kept),
            context={
                "draft_version": draft.version,
                "prompt_name": system.name,
                "prompt_version": system.version,
                "prompt_sha256": system.sha256,
            },
        )

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
            rubric=out.rubric.model_dump(),
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
            prompt_version=system.version,
        )
        from app.core.cost_tracking import current_cost

        return {
            "latest_verdict": verdict,
            "debate_round": debate_round,
            "cost_so_far": current_cost(),
            "prompt_versions": {system.name: system.version},
            "transcript": [msg],
        }
