"""Writer worker — drafts and revises the report, citing real evidence.

Every run appends a new versioned ``ReportDraft``; revision runs receive the
Reviewer's feedback explicitly. Citations carry verbatim quotes so the
Reviewer can verify them against fetched source text.
"""

import re
from typing import Any

import structlog
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

logger = structlog.get_logger()


class DraftCitation(BaseModel):
    claim: str
    quote: str = Field(description="Verbatim text copied from the source")
    source_url: str


class PassageCitation(BaseModel):
    """Extractive citation: point at a passage id, never retype a quote."""

    claim: str
    # Pattern constraint lands in the JSON schema — structured output
    # cannot emit "financial_analyst" instead of a real passage id.
    passage_id: str = Field(
        pattern=r"S\d+-P\d+",
        description="Passage id from the evidence block, e.g. S0-P2",
    )


class DraftOutput(BaseModel):
    title: str
    markdown: str
    citations: list[DraftCitation] = Field(default_factory=list)


class DraftOutputPassages(BaseModel):
    """Extractive-mode output — same shape, passage ids instead of quotes.

    Field order is load-bearing: the model emits ``citations`` BEFORE
    ``markdown``, so it must select the passage facts it will cite first and
    then write sentences around them — cite-then-write, not write-then-cite.
    Prose drafted before citation selection drifts into inference and the
    claim judge flags it.
    """

    title: str
    citations: list[PassageCitation] = Field(default_factory=list)
    markdown: str


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "the", "a", "an", "of", "to", "in", "and", "or", "for", "with",
        "by", "on", "as", "at", "is", "are", "was", "were", "be", "been",
        "that", "this", "it", "its", "from", "into", "can", "could", "may",
        "might", "should", "would", "will", "shall", "not", "no", "than",
        "then", "so", "such", "also", "more", "most", "other", "others",
        "over", "under", "between", "their", "his", "her", "our", "your",
        "we", "they", "he", "she", "you", "i",
    ]
)
# A citation's claim must restate the quote — if most of its content words
# are absent from the quoted passage, the claim is the Writer's inference
# attached to a topical passage (the class the claim judge flags). Sixty
# percent tolerates synonym rephrasing ("annual fee" ~ "per year").
_CLAIM_COVERAGE_MIN = 0.6


def _claim_coverage(claim: str, quote: str) -> float:
    """Share of the claim's content tokens present in the quoted passage."""
    claim_tokens = [
        t for t in _TOKEN_RE.findall(claim.lower()) if t not in _STOPWORDS
    ]
    if not claim_tokens:
        return 1.0
    quote_tokens = set(_TOKEN_RE.findall(quote.lower()))
    hits = sum(1 for t in claim_tokens if t in quote_tokens)
    return hits / len(claim_tokens)


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
        # Generous budget: the Writer drafts one report and should see
        # enough distinct facts to build depth — starving it to ~9 passages
        # produced thin reports that repeat the same two sources.
        index = build_passage_index(evidence, query, per_source_k=4, char_budget=10000)
        ev_block = render_passages(index)
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none)"
        template = SECTION_TEMPLATES.get(
            brief.report_type, SECTION_TEMPLATES["general"]
        )
        # Numbered heading list, not the "·"-joined string — an inline list
        # let the model merge or drop sections (observed live: missing
        # "Analysis" cost structure=3 and the round).
        section_list = "\n".join(
            f"{i}. {s}" for i, s in enumerate(template.split(" · "), 1)
        )
        tenant = (
            f"Client context (orient the report to them): "
            f"{brief.tenant_context}\n"
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
            notes_block = (
                "\n\nSpecialist analyst notes (internal context — use for "
                "orientation, but NEVER cite an analyst name as a source; "
                "cite the evidence passage that states the fact):\n"
                f"{rendered}"
            )

        feedback = ""
        if verdict is not None and (verdict.feedback or state.get("drafts")):
            items = "\n".join(f"- {f}" for f in verdict.feedback)
            prev = state.get("drafts", [])
            prev_md = prev[-1].markdown[:6000] if prev else "(none)"
            prev_cites = ""
            if prev and prev[-1].citations:
                listed = "\n".join(
                    f'- "{c.claim}" → {c.ref or c.source_url}'
                    for c in prev[-1].citations
                    if c.ref
                )
                if listed:
                    prev_cites = (
                        "\nAlready-adjudicated citation entries — reuse these "
                        "claim/passage_id pairs VERBATIM for every sentence you "
                        "keep; do not re-write them:\n" + listed
                    )
            feedback = (
                f"\n\nThis is revision round {rounds + 1}. Your previous "
                f"draft is below — apply MINIMAL, targeted edits and keep "
                f"every citation that was already verified.\n"
                f"--- previous draft v{len(prev)} ---\n{prev_md}\n"
                f"--- end previous draft ---"
                f"{prev_cites}\n"
                f"The Reviewer's feedback you MUST address:\n{items}"
            )
        cite_rule = (
            "\n\nCitations — CITE FIRST, THEN WRITE: you emit `citations` "
            "before `markdown`, so FIRST pick the 6–10 passage facts the "
            "report will stand on (claim in the passage's own words, "
            "passage_id like S0-P2), THEN write the markdown so each chosen "
            "claim appears as a sentence carrying its marker inline, e.g. "
            "`... 69% of firms report X [S0-P2].` The passage text must "
            "directly state the claim — no numbers, names or scope beyond "
            "it. Do NOT retype quotes; the system attaches the exact text. "
            "Never cite a passage that only mentions the topic; prefer "
            "passages containing the concrete fact — numbers, dates, named "
            "companies, measured outcomes over qualitative filler. "
            "Specialist analyst "
            "notes are context, not evidence — the only valid citation "
            "targets are `[Sx-Py]` passage ids."
            if self._citation_mode == "extractive"
            else ""
        )
        return (
            f"Topic: {brief.topic}\nAudience: {brief.audience}\n"
            f"Tone: {brief.tone}\nLanguage: {brief.language}\n"
            f"Report type: {brief.report_type}\n"
            f"Required sections — every one MUST appear as a `##` heading, "
            f"spelled exactly as listed, in this order:\n{section_list}\n"
            f"(If a `Sources` section is listed, emit the heading with an "
            f"empty body — the source list is generated automatically.)\n"
            f"{tenant}\n"
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
        dead_markers: list[str] = []
        # (old pid, new pid) pairs in document order — markers are rewritten
        # positionally, like dead-marker removal.
        remapped: list[tuple[str, str]] = []
        for c in out.citations:
            if not extractive:
                citations.append(
                    Citation(claim=c.claim, quote=c.quote, source_url=c.source_url)
                )
                continue
            # The model sometimes emits the bracketed marker form
            # ("[S9-P9]") — the schema's pattern is re.search-based so it
            # validates. Normalize to the bare id so resolve() and marker
            # rewriting see the same string.
            raw_pid = getattr(c, "passage_id", "")
            m = re.search(r"S\d+-P\d+", raw_pid)
            pid = m.group(0) if m else raw_pid
            p = index.resolve(pid)
            best = max(
                index.by_id.values(),
                key=lambda q: _claim_coverage(c.claim, q.text),
                default=None,
            )
            best_cov = _claim_coverage(c.claim, best.text) if best else 0.0
            remaps = (
                # The emitted id never existed — but another passage may
                # state the claim (the model mistyped an index). Remap to a
                # clear match.
                (p is None and best_cov >= _CLAIM_COVERAGE_MIN)
                # Right claim, wrong passage: the model attached a
                # topically related id rather than the one stating the
                # fact. Re-point at the clearly better passage — the quote
                # stays verbatim evidence text. If nothing clearly states
                # the claim, it stays as cited and the claim adjudicator
                # decides.
                or (
                    p is not None
                    and best is not None
                    and best.pid != pid
                    and best_cov >= _CLAIM_COVERAGE_MIN
                    and best_cov - _claim_coverage(c.claim, p.text) >= 0.2
                )
            )
            if remaps and best is not None:
                logger.info(
                    "citation_remapped",
                    old=pid, new=best.pid, claim=c.claim[:120],
                )
                remapped.append((pid, best.pid))
                p = best
            if p is None:
                # No passage states the claim — keep it visible as an
                # unknown-URL citation, exactly like a hallucinated source.
                citations.append(
                    Citation(
                        claim=c.claim, quote="",
                        source_url=f"passage:{pid or '?'}",
                    )
                )
                dead_markers.append(pid)
                continue
            citations.append(
                Citation(claim=c.claim, quote=p.text, source_url=p.url, ref=p.pid)
            )
        logger.info(
            "writer_citations",
            emitted=len(out.citations),
            kept=len(citations),
        )
        markdown = out.markdown
        # Marker edits are positional: claims are emitted in document order,
        # so each dropped/remapped entry owns the LAST occurrence of its
        # marker — earlier ones may belong to kept citations on the same id.
        def _last_marker(text: str, pid: str) -> tuple[str, int]:
            i = text.rfind(f" [{pid}]")
            if i != -1:
                return f" [{pid}]", i
            i = text.rfind(f"[{pid}]")
            return f"[{pid}]", i

        # Dead markers go first — they operate on the model's original
        # markers; a remapped marker may reuse a pid that also has a dead
        # entry, and it must not exist yet when removals run.
        for pid in dead_markers:
            marker, i = _last_marker(markdown, pid)
            if i != -1:
                markdown = markdown[:i] + markdown[i + len(marker):]
        for old, new in remapped:
            marker, i = _last_marker(markdown, old)
            if i != -1:
                markdown = (
                    markdown[:i] + marker.replace(old, new) + markdown[i + len(marker):]
                )
        draft = ReportDraft(
            version=version,
            title=out.title,
            markdown=markdown,
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
