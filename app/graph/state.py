"""Domain models and LangGraph state for report orchestration.

``OrchestrationState`` is the LangGraph ``TypedDict`` flowing through the
graph; the Pydantic models are the typed payload carried inside it.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal, TypedDict, TypeVar

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "awaiting_review", "done", "failed", "blocked"]
AgentRole = Literal["supervisor", "researcher", "writer", "reviewer"]
Verdict = Literal["approve", "revise"]

_T = TypeVar("_T")


def accumulate(existing: list[_T] | None, incoming: list[_T]) -> list[_T]:
    """LangGraph reducer: append ``incoming`` items to the accumulated list."""
    return [*(existing or []), *incoming]


class Brief(BaseModel):
    """User-supplied report brief — the input contract for an orchestration job."""

    topic: str = Field(min_length=3, max_length=500)
    audience: str = Field(default="general business", max_length=200)
    tone: str = Field(default="analytical", max_length=100)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="en", max_length=10)


class EvidenceItem(BaseModel):
    """A piece of fetched evidence attributable to a real source."""

    evidence_id: str
    url: str
    title: str = ""
    text: str = ""
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    query: str = ""


class Citation(BaseModel):
    """A claim in the report backed by a verbatim quote from a source."""

    claim: str
    quote: str
    source_url: str


class ReportDraft(BaseModel):
    """A versioned report draft — every Writer revision appends a new version."""

    version: int = Field(ge=1)
    title: str
    markdown: str
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewVerdict(BaseModel):
    """The Reviewer's structured verdict on a draft."""

    draft_version: int = Field(ge=0)  # 0 = "no draft exists yet"
    verdict: Verdict
    score: float = Field(ge=1.0, le=5.0)
    rubric: dict[str, float] = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list)
    unsupported_citations: int = Field(default=0, ge=0)


class AgentMessage(BaseModel):
    """One entry in the inter-agent transcript (the Supervisor-Workers debate)."""

    sender: str
    recipient: str
    kind: Literal["dispatch", "result", "feedback", "verdict", "escalation", "system"]
    content: str
    round: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrchestrationState(TypedDict, total=False):
    """LangGraph state shared across Supervisor and Worker nodes."""

    job_id: str
    workspace_id: str
    brief: Brief
    status: JobStatus
    sub_questions: list[str]
    evidence: Annotated[list[EvidenceItem], accumulate]
    drafts: Annotated[list[ReportDraft], accumulate]
    transcript: Annotated[list[AgentMessage], accumulate]
    debate_round: int
    max_debate_rounds: int
    latest_verdict: ReviewVerdict | None
    next_worker: str | None
    final_report: str | None
    errors: Annotated[list[str], accumulate]
