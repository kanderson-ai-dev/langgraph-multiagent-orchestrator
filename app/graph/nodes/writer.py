"""Writer worker — drafts and revises the report, citing real evidence.

Every run appends a new versioned ``ReportDraft``; revision runs receive the
Reviewer's feedback explicitly. Citations carry verbatim quotes so the
Reviewer can verify them against fetched source text.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.framework.worker import StateUpdate
from app.graph.prompts import WRITER_SYSTEM
from app.graph.state import AgentMessage, Brief, Citation, EvidenceItem, ReportDraft
from app.services.llm_client import StructuredLLM


class DraftCitation(BaseModel):
    claim: str
    quote: str = Field(description="Verbatim text copied from the source")
    source_url: str


class DraftOutput(BaseModel):
    title: str
    markdown: str
    citations: list[DraftCitation] = Field(default_factory=list)


class WriterWorker:
    name = "writer"
    description = "Drafts/revises the report in Markdown with verifiable citations"

    def __init__(self, llm: StructuredLLM) -> None:
        self._llm = llm

    def _prompt(self, state: dict[str, Any]) -> str:
        brief: Brief = state["brief"]
        evidence: list[EvidenceItem] = state.get("evidence", [])
        verdict = state.get("latest_verdict")
        rounds = state.get("debate_round", 0)

        ev_block = "\n\n".join(
            f"[source {i}] {e.url}\n{e.text[:1500]}" for i, e in enumerate(evidence)
        ) or "(no evidence gathered)"
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"

        feedback = ""
        if verdict is not None and verdict.feedback:
            items = "\n".join(f"- {f}" for f in verdict.feedback)
            feedback = (
                f"\n\nThis is revision round {rounds + 1}. The Reviewer's "
                f"feedback you MUST address:\n{items}"
            )
        return (
            f"Topic: {brief.topic}\nAudience: {brief.audience}\n"
            f"Tone: {brief.tone}\nLanguage: {brief.language}\n"
            f"Requirements:\n{reqs}\n\n"
            f"Evidence (cite only from these sources):\n{ev_block}"
            f"{feedback}"
        )

    async def run(self, state: dict[str, Any]) -> StateUpdate:
        brief: Brief = state["brief"]
        version = len(state.get("drafts", [])) + 1
        from app.core.cost_tracking import tracked_structured

        out = await tracked_structured(
            self._llm,
            DraftOutput,
            role=self.name,
            model="",
            system=WRITER_SYSTEM,
            user=self._prompt(state),
            context={"brief": brief.model_dump(), "version": version},
        )
        draft = ReportDraft(
            version=version,
            title=out.title,
            markdown=out.markdown,
            citations=[
                Citation(claim=c.claim, quote=c.quote, source_url=c.source_url)
                for c in out.citations
            ],
        )
        action = "revised" if version > 1 else "drafted"
        msg = AgentMessage(
            sender=self.name,
            recipient="reviewer",
            kind="result",
            content=f"{action} report v{version}: {draft.title}",
            round=state.get("debate_round", 0),
        )
        return {"drafts": [draft], "transcript": [msg]}
