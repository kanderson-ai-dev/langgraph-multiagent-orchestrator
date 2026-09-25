"""Researcher worker — gathers verifiable evidence for the brief.

Plans focused sub-questions (LLM), searches the web, scrapes top results and
user-supplied source URLs through the polite client, parses documents, and
emits ``EvidenceItem``s traceable to a real source URL.
"""

import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.framework.worker import StateUpdate
from app.graph.prompts import RESEARCHER_SYSTEM
from app.graph.state import AgentMessage, Brief, EvidenceItem
from app.services.document_parser import parse_document
from app.services.llm_client import StructuredLLM
from app.services.scraper_client import ScraperClient
from app.services.search_client import SearchClient

logger = get_logger(__name__)

_MAX_EVIDENCE_CHARS = 8_000


class ResearchPlan(BaseModel):
    """LLM-produced research plan for a brief."""

    sub_questions: list[str] = Field(default_factory=list, max_length=10)
    queries: list[str] = Field(default_factory=list, max_length=10)


class ResearcherWorker:
    name = "researcher"
    description = "Plans sub-questions and gathers cited web/document evidence"

    def __init__(
        self,
        llm: StructuredLLM,
        search: SearchClient,
        scraper: ScraperClient,
        *,
        max_queries: int = 5,
        max_results_per_query: int = 2,
    ) -> None:
        self._llm = llm
        self._search = search
        self._scraper = scraper
        self._max_queries = max_queries
        self._max_results = max_results_per_query

    def _plan_prompt(self, brief: Brief) -> str:
        reqs = "\n".join(f"- {r}" for r in brief.requirements) or "- (none given)"
        return (
            f"Report topic: {brief.topic}\n"
            f"Audience: {brief.audience}\nTone: {brief.tone}\n"
            f"Requirements:\n{reqs}\n"
            f"Language: {brief.language}"
        )

    async def _gather_url(self, url: str, query: str) -> EvidenceItem | None:
        try:
            page = await self._scraper.scrape(url)
        except Exception as exc:
            logger.info("scrape_skipped", url=url, reason=str(exc))
            return None
        doc = parse_document(
            page.body, content_type=page.content_type, url=str(page.url)
        )
        if not doc.text:
            return None
        return EvidenceItem(
            evidence_id=str(uuid.uuid4()),
            url=str(page.url),
            title=doc.title,
            text=doc.text[:_MAX_EVIDENCE_CHARS],
            query=query,
        )

    async def run(self, state: dict[str, Any]) -> StateUpdate:
        brief: Brief = state["brief"]
        plan = await self._llm.structured(
            ResearchPlan,
            system=RESEARCHER_SYSTEM.format(max_queries=self._max_queries),
            user=self._plan_prompt(brief),
            context={"brief": brief.model_dump()},
        )
        queries = plan.queries[: self._max_queries]

        candidate_urls: list[tuple[str, str]] = []  # (url, query)
        for url in brief.source_urls:  # user-supplied sources first
            candidate_urls.append((url, "user-supplied"))
        for q in queries:
            try:
                results = await self._search.search(
                    q, max_results=self._max_results
                )
            except Exception as exc:
                logger.warning("search_failed", query=q, error=str(exc))
                continue
            candidate_urls.extend((r.url, q) for r in results)

        evidence: list[EvidenceItem] = []
        for url, q in candidate_urls:
            item = await self._gather_url(url, q)
            if item is not None:
                evidence.append(item)

        msg = AgentMessage(
            sender=self.name,
            recipient="supervisor",
            kind="result",
            content=(
                f"Gathered {len(evidence)} evidence items "
                f"across {len(queries)} queries."
            ),
        )
        return {
            "sub_questions": plan.sub_questions,
            "evidence": evidence,
            "transcript": [msg],
        }
