"""Reviewer/Auditor worker — grades the draft and verifies every citation.

Two-stage audit:
1. mechanical: each citation's quote must be verifiably present in the
   fetched source text (``citation_supported``) — unsupported quotes are
   dropped and counted;
2. judgmental: the LLM scores the rubric and issues ``approve``/``revise``.

A support rate below threshold forces ``revise`` — quality is measured,
never assumed.
"""

import re
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.core.metrics import DEBATE_ROUNDS_TOTAL

# Same constant the EDD gate enforces — single source in app.core.thresholds.
from app.core.thresholds import EDD_THRESHOLDS
from app.framework.worker import StateUpdate
from app.graph.citations import citation_supported, support_rate
from app.graph.claim_support import judge_claim_support
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
# In-loop ship bar for thin support: a draft may carry 'partly' claims
# while fully-supported citations are a solid majority — the published
# gate is a cross-case average, not a per-draft perfection requirement.
_CLAIM_SHIP_MIN = 2 / 3


def _citation_feedback(dropped: int, unknown: int, rate: float) -> str:
    """Reviewer feedback the Writer can act on — names the failure kind."""
    base = (
        f"{dropped} citation(s) could not be verified against the fetched "
        f"source text (support rate {rate:.2f}); re-cite or remove them."
    )
    if unknown:
        base += (
            f" {unknown} of them cite URL(s) not in the evidence set — fix "
            "the source URL to one that was actually fetched."
        )
    return base


_MARKER_RE = re.compile(r"\[S\d+-P\d+\]")


def _grounding_audit(
    draft: ReportDraft, kept: list[Citation]
) -> tuple[float, str]:
    """Deterministic grounding check for extractive (marker) drafts.

    Grounding = "every factual claim traceable to a supported citation".
    With inline `[Sx-Py]` markers that is mechanically decidable: a marker
    either resolves to a verified citation or it does not. The *semantic*
    half — does the cited claim actually cover the sentence — is measured
    by the independent claim-support judge, not guessed here.

    Returns (score, audit_note). Score anchors: 5 = every marker resolves;
    <=3 = citations exist but text is untraceable / markers unresolved;
    2 = no citations at all.
    """
    markers = {m.strip("[]") for m in _MARKER_RE.findall(draft.markdown)}
    if not draft.citations and not markers:
        return 2.0, "no citations and no inline markers"
    if not markers:
        return 3.0, (
            f"{len(draft.citations)} citation(s) but no inline markers — "
            "claims are not traceable to the text"
        )
    kept_refs = {c.ref for c in kept if c.ref}
    unresolved = sorted(markers - kept_refs)
    if unresolved:
        return (
            max(2.0, 5.0 - 1.5 * len(unresolved)),
            f"{len(markers)} inline marker(s), unresolved: "
            f"{', '.join(unresolved)}",
        )
    return (
        5.0,
        f"all {len(markers)} inline marker(s) resolve to supported citations",
    )


def _norm_heading(line: str) -> str:
    h = line.lstrip("#").strip().lower()
    return " ".join(h.split()).rstrip(":.–-").strip()


def _structure_score(draft: ReportDraft, template: str) -> float:
    """Deterministic structure grade.

    The Writer's contract is objective — every template section present as
    a heading, in order — so it is checked mechanically rather than judged.
    Anchors: 5 = exact template; <=3 = sections present with order/gaps;
    2 = template ignored. Any heading level counts (the criterion is the
    section existing, not its depth).
    """
    required = [_norm_heading(s) for s in template.split("·") if s.strip()]
    if not required:
        return 5.0
    headings = [
        _norm_heading(ln)
        for ln in draft.markdown.splitlines()
        if ln.lstrip().startswith("#")
    ]
    matched = 0
    for h in headings:
        if matched < len(required) and h == required[matched]:
            matched += 1
    if matched == len(required):
        return 5.0
    return min(3.0, 2.0 + matched / len(required))


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
) -> tuple[list[Citation], int, list[str], int]:
    """Split citations into (supported, dropped, hashes, unknown-urls).

    ``kept`` and ``source_hashes`` are aligned: each supported citation
    records the ``sha256`` of the evidence item that verified it (empty
    string for pre-seal legacy items). ``unknown`` counts the dropped
    citations whose URL isn't in the evidence set at all — surfaced in the
    reviewer feedback so a revision can fix the URL instead of re-quoting.

    A citation verifies only against the source it *cites* — never against
    pooled evidence text. A quote that exists in source B but is credited
    to source A (or to a phantom URL) is contamination, not support.
    """
    by_url = {e.url: e for e in evidence}
    kept: list[Citation] = []
    source_hashes: list[str] = []
    dropped = 0
    unknown = 0
    for c in draft.citations:
        item = by_url.get(c.source_url)
        if item is not None and citation_supported(c.quote, item.text):
            kept.append(c)
            source_hashes.append(item.sha256)
        else:
            dropped += 1
            if item is None:
                unknown += 1
    return kept, dropped, source_hashes, unknown


class ReviewerWorker:
    name = "reviewer"
    description = "Audits drafts against the rubric and verifies citations"

    def __init__(
        self,
        llm: StructuredLLM,
        prompts: PromptRegistry,
        *,
        citation_shortcircuit: bool = True,
        claim_judge: StructuredLLM | None = None,
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._citation_shortcircuit = citation_shortcircuit
        # The in-loop claim adjudicator must apply the same strictness the
        # eval gate measures — so it runs on the judge model (gpt-4o), not
        # the worker model. Falls back to the worker LLM offline so stub
        # builders still drive tests.
        self._claim_judge = claim_judge or llm

    def _prompt(
        self,
        brief: Brief,
        draft: ReportDraft,
        kept: list[Citation],
        *,
        structure_score: float,
        grounding_note: str,
    ) -> str:
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"
        cites = "\n".join(
            f"- [{c.ref or c.source_url}] {c.claim} (quote: {c.quote[:120]}…)"
            for c in kept
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
            f"Expected section structure: {template}\n"
            f"Mechanical structure check (authoritative — headings verified "
            f"in code): structure = {structure_score:.1f}/5. Do NOT dispute "
            f"section presence in feedback; the check already ran.\n"
            f"Inline `[Sx-Py]` markers in the markdown are citation ids — a "
            f"sentence carrying one cites the matching entry in the "
            f"supported-citations list below. Grounding check: a factual "
            f"sentence is grounded iff its marker appears below AND that "
            f"entry's claim covers it; unmarked factual sentences are the "
            f"defect to flag.\n"
            f"Mechanical marker audit (authoritative): {grounding_note}.\n\n"
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
        kept, dropped, _hashes, unknown = verify_citations(draft, evidence)
        DEBATE_ROUNDS_TOTAL.inc()
        rate = support_rate(len(kept), len(draft.citations))

        if (
            self._citation_shortcircuit
            and dropped
            and rate < _SUPPORT_THRESHOLD
        ):
            feedback = [
                _citation_feedback(dropped, unknown, rate),
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

        # Semantic adjudication: a quote that exists is not a claim the
        # quote supports. Citations judged weak block approval until the
        # Writer re-cites or removes them — claim integrity is checked in
        # the loop, not assumed. Empty verdicts (offline stubs) are a no-op.
        claim_verdicts, claim_rate = await judge_claim_support(
            kept, judge_llm=self._claim_judge
        )
        structlog.get_logger().info(
            "claim_adjudication",
            draft_version=draft.version,
            verdicts=claim_verdicts,
            rate=round(claim_rate, 3),
        )
        weak_claims = [
            c
            for c, v in zip(kept, claim_verdicts, strict=False)
            if v != "yes"
        ]
        # 'no' = the quote is unrelated or contradicting — a wrong citation,
        # never shippable. 'partly' = thin support — tolerable while clearly
        # a minority: the published gate is an AVERAGE over cases (≥0.85),
        # so demanding that share per-draft is stricter than the contract
        # and makes thin-evidence cases non-convergent by construction
        # (1 partly in 6 citations is 0.833 — unreachable). The ship bar:
        # nothing wrong cited AND a solid majority fully supported.
        no_claims = [
            c
            for c, v in zip(kept, claim_verdicts, strict=False)
            if v == "no"
        ]
        claims_ok = not claim_verdicts or (
            not no_claims and claim_rate >= _CLAIM_SHIP_MIN
        )

        template = SECTION_TEMPLATES.get(
            brief.report_type, SECTION_TEMPLATES["general"]
        )
        structure = _structure_score(draft, template)
        grounding, grounding_note = _grounding_audit(draft, kept)
        mechanical_grounding = bool(_MARKER_RE.search(draft.markdown))
        system = self._prompts.render("reviewer")
        out = await tracked_structured(
            self._llm,
            ReviewOutput,
            role=self.name,
            system=system.text,
            user=self._prompt(
                brief, draft, kept,
                structure_score=structure, grounding_note=grounding_note,
            ),
            context={
                "draft_version": draft.version,
                "prompt_name": system.name,
                "prompt_version": system.version,
                "prompt_sha256": system.sha256,
            },
        )

        # Verdict is computed in code from the rubric — the LLM's own
        # "approve/revise" is advisory (kept in feedback when it disagrees).
        # Approve iff: citation gate passes AND every rubric dimension
        # >= 3.5 AND the rubric mean >= 4.0 (the published bars).
        # The structure dimension is overridden by the mechanical heading
        # check — the LLM repeatedly hallucinated missing sections on drafts
        # that contained every required heading verbatim.
        out.rubric = Rubric(
            structure=structure,
            clarity=out.rubric.clarity,
            # Marker-traceable drafts get the mechanical audit; free-quote
            # drafts keep the LLM's grounding judgment.
            grounding=grounding if mechanical_grounding else out.rubric.grounding,
            tone=out.rubric.tone,
        )
        rubric_vals = [
            out.rubric.structure, out.rubric.clarity,
            out.rubric.grounding, out.rubric.tone,
        ]
        rubric_mean = sum(rubric_vals) / len(rubric_vals)
        approve = (
            rate >= _SUPPORT_THRESHOLD
            and min(rubric_vals) >= 3.5
            and rubric_mean >= 4.0
            and claims_ok
        )
        verdict_str: Verdict = "approve" if approve else "revise"
        if (out.verdict == "approve") != approve:
            feedback = [
                f"(advisory) model verdict was {out.verdict!r}; "
                f"rule-derived verdict is {verdict_str}"
            ]
        else:
            feedback = []
        feedback += list(out.feedback)
        if weak_claims:
            blocking = "; ".join(
                f"[{c.ref or c.source_url}] {c.claim[:80]}" for c in no_claims
            )
            partly = "; ".join(
                f"[{c.ref or c.source_url}] {c.claim[:80]}"
                for c, v in zip(kept, claim_verdicts, strict=False)
                if v == "partly"
            )
            feedback.append(
                f"Cited claims not fully supported by their quoted passage "
                f"(claim-support {claim_rate:.2f}). "
                + (
                    f"BLOCKING — unrelated or contradicting citations, "
                    f"remove them or delete the claim: {blocking}. "
                    if no_claims
                    else ""
                )
                + (
                    f"Thin support — restate the claim as the quote's "
                    f"literal content or remove it: {partly}."
                    if partly
                    else ""
                )
            )
        if dropped:
            feedback.append(_citation_feedback(dropped, unknown, rate))

        verdict = ReviewVerdict(
            draft_version=draft.version,
            verdict=verdict_str,
            # The published metric is the rubric mean — derive it in code
            # rather than trusting the model's holistic self-score.
            score=min(max(rubric_mean, 1.0), 5.0),
            rubric=out.rubric.model_dump(),
            feedback=feedback,
            unsupported_citations=dropped,
            weak_claims=len(weak_claims),
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
