"""Writer worker — drafts and revises the report, citing real evidence.

Every run appends a new versioned ``ReportDraft``; revision runs receive the
Reviewer's feedback explicitly. Citations carry verbatim quotes so the
Reviewer can verify them against fetched source text.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.framework.worker import StateUpdate
from app.graph.passages import PassageIndex, build_passage_index, render_passages
from app.graph.state import (
    AgentMessage,
    AnalystNote,
    Brief,
    Citation,
    EvidenceItem,
    ReportDraft,
)
from app.graph.teams import SECTION_TEMPLATES
from app.services.llm_client import StructuredLLM
from app.services.prompt_registry import PromptRegistry


class DraftCitation(BaseModel):
    claim: str
    quote: str = Field(description="Verbatim text copied from the source")
    source_url: str


class PassageCitation(BaseModel):
    """Extractive citation: point at a passage id, never retype a quote."""

    claim: str
    passage_id: str = Field(
        description="Passage id from the evidence block, e.g. S0-P2"
    )


class DraftOutput(BaseModel):
    title: str
    markdown: str
    citations: list[DraftCitation] = Field(default_factory=list)


class DraftOutputPassages(BaseModel):
    """Extractive-mode output — same shape, passage ids instead of quotes."""

    title: str
    markdown: str
    citations: list[PassageCitation] = Field(default_factory=list)


class WriterWorker:
    name = "writer"
    description = "Drafts/revises the report in Markdown with verifiable citations"

    def __init__(
        self,
        llm: StructuredLLM,
        prompts: PromptRegistry,
        *,
        citation_mode: str = "free_quote",
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._citation_mode = citation_mode

    def _prompt(self, state: dict[str, Any]) -> tuple[str, PassageIndex]:
        brief: Brief = state["brief"]
        evidence: list[EvidenceItem] = state.get("evidence", [])
        verdict = state.get("latest_verdict")
        rounds = state.get("debate_round", 0)

        # Ranked passages instead of raw text[:1500] — the first slice of a
        # page is usually boilerplate; the BM25 ranking surfaces the parts
        # that actually answer the brief. Verification still runs against
        # the full EvidenceItem text downstream.
        query = " ".join(
            [brief.topic, *brief.requirements, *state.get("sub_questions", [])]
        )
        index = build_passage_index(evidence, query)
        ev_block = render_passages(index)
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"
        template = SECTION_TEMPLATES.get(
            brief.report_type, SECTION_TEMPLATES["general"]
        )
        tenant = (
            f"\nClient context (orient the report to them): {brief.tenant_context}"
            if brief.tenant_context
            else ""
        )

        notes: list[AnalystNote] = state.get("analyst_notes", [])
        notes_block = ""
        if notes:
            rendered = "\n".join(
                f"[{n.analyst}] findings: {'; '.join(n.findings)}; "
                f"flags: {'; '.join(n.flags)}; recommendation: {n.recommendation}"
                for n in notes
            )
            notes_block = f"\n\nSpecialist analyst notes:\n{rendered}"

        feedback = ""
        if verdict is not None and (verdict.feedback or state.get("drafts")):
            items = "\n".join(f"- {f}" for f in verdict.feedback)
            prev = state.get("drafts", [])
            prev_md = prev[-1].markdown[:6000] if prev else "(none)"
            feedback = (
                f"\n\nThis is revision round {rounds + 1}. Your previous "
                f"draft is below — apply MINIMAL, targeted edits and keep "
                f"every citation that was already verified.\n"
                f"--- previous draft v{len(prev)} ---\n{prev_md}\n"
                f"--- end previous draft ---\n"
                f"The Reviewer's feedback you MUST address:\n{items}"
            )
        cite_rule = (
            "\n\nCitations: for every factual claim, cite the passage id "
            "(e.g. S0-P2) shown in brackets before the passage — do NOT "
            "retype quotes; the system attaches the exact text."
            if self._citation_mode == "extractive"
            else ""
        )
        return (
            f"Topic: {brief.topic}\nAudience: {brief.audience}\n"
            f"Tone: {brief.tone}\nLanguage: {brief.language}\n"
            f"Report type: {brief.report_type}\n"
            f"Required section structure: {template}{tenant}\n"
            f"Requirements:\n{reqs}\n\n"
            f"Evidence (cite only from these sources):\n{ev_block}"
            f"{notes_block}"
            f"{feedback}"
            f"{cite_rule}"
        ), index

    async def run(self, state: dict[str, Any]) -> StateUpdate:
        brief: Brief = state["brief"]
        version = len(state.get("drafts", [])) + 1
        from app.core.cost_tracking import tracked_structured

        system = self._prompts.render("writer")
        prompt_text, index = self._prompt(state)
        extractive = self._citation_mode == "extractive"
        out = await tracked_structured(
            self._llm,
            DraftOutputPassages if extractive else DraftOutput,
            role=self.name,
            system=system.text,
            user=prompt_text,
            context={
                "brief": brief.model_dump(),
                "version": version,
                "prompt_name": system.name,
                "prompt_version": system.version,
                "prompt_sha256": system.sha256,
            },
        )
        citations: list[Citation] = []
        for c in out.citations:
            if extractive:
                p = index.resolve(getattr(c, "passage_id", ""))
                if p is not None:
                    citations.append(
                        Citation(claim=c.claim, quote=p.text, source_url=p.url)
                    )
                else:
                    # Unknown id → counted as an unknown-URL citation by the
                    # verifier, exactly like a hallucinated source.
                    citations.append(
                        Citation(
                            claim=c.claim,
                            quote="",
                            source_url=f"passage:{getattr(c, 'passage_id', '?')}",
                        )
                    )
            else:
                citations.append(
                    Citation(claim=c.claim, quote=c.quote, source_url=c.source_url)
                )
        draft = ReportDraft(
            version=version,
            title=out.title,
            markdown=out.markdown,
            citations=citations,
        )
        action = "revised" if version > 1 else "drafted"
        msg = AgentMessage(
            sender=self.name,
            recipient="reviewer",
            kind="result",
            content=f"{action} report v{version}: {draft.title}",
            round=state.get("debate_round", 0),
            prompt_version=system.version,
        )
        from app.core.cost_tracking import current_cost

        return {
            "drafts": [draft],
            "cost_so_far": current_cost(),
            "prompt_versions": {system.name: system.version},
            "transcript": [msg],
        }
