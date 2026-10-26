"""Domain models and LangGraph state for report orchestration.

``OrchestrationState`` is the LangGraph ``TypedDict`` flowing through the
graph; the Pydantic models are the typed payload carried inside it.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypedDict, TypeVar

from pydantic import BaseModel, Field

JobStatus = Literal[
    "queued", "running", "awaiting_review", "done", "failed", "blocked",
    "expired",
]
Verdict = Literal["approve", "revise"]

# Report types a tenant can order; each maps to a different agent team.
ReportType = Literal[
    "general",
    "market_intelligence",
    "vendor_assessment",
    "competitive_landscape",
    "technical_due_diligence",
]

_T = TypeVar("_T")


def accumulate(existing: list[_T] | None, incoming: list[_T]) -> list[_T]:
    """LangGraph reducer: append ``incoming`` items to the accumulated list."""
    return [*(existing or []), *incoming]


def keep_max(existing: float | None, incoming: float) -> float:
    """Reducer for monotonic values (cost): parallel writes keep the max."""
    return max(existing or 0.0, incoming)


def merge_dicts(
    existing: dict[str, int] | None, incoming: dict[str, int]
) -> dict[str, int]:
    """Reducer for prompt provenance: parallel workers each add their own
    ``prompt name → version`` entry."""
    return {**(existing or {}), **incoming}


class Brief(BaseModel):
    """User-supplied report brief — the input contract for an orchestration job."""

    topic: str = Field(min_length=3, max_length=500)
    audience: str = Field(default="general business", max_length=200)
    tone: str = Field(default="analytical", max_length=100)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="en", max_length=10)
    report_type: ReportType = "general"
    tenant_context: str = Field(
        default="",
        max_length=2000,
        description="Client company profile that orients research and writing",
    )
    budget_usd: float | None = Field(
        default=None, ge=0.0,
        description="Hard LLM-spend cap for this job; None = workspace default",
    )


class EvidenceItem(BaseModel):
    """A piece of fetched evidence attributable to a real source."""

    evidence_id: str
    url: str
    title: str = ""
    text: str = ""
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    query: str = ""
    # Content addressing (Phase 6): sha256 of url|content_type|sanitized
    # text, computed at gather time. Empty for pre-seal legacy items.
    sha256: str = ""
    content_type: str = ""


class Citation(BaseModel):
    """A claim in the report backed by a verbatim quote from a source.

    ``ref`` is the passage id (``S<i>-P<j>``) the claim cites in extractive
    mode — the same marker the Writer puts inline in the markdown, so a
    reader can resolve marker → claim → source.
    """

    claim: str
    quote: str
    source_url: str
    ref: str = ""


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
    # Citations whose quote exists but does not support the claim —
    # counted by the reviewer's semantic adjudication.
    weak_claims: int = Field(default=0, ge=0)


class AnalystNote(BaseModel):
    """A specialist worker's structured take on the gathered evidence."""

    analyst: str
    findings: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    recommendation: str = ""


class AgentMessage(BaseModel):
    """One entry in the inter-agent transcript (the Supervisor-Workers debate)."""

    sender: str
    recipient: str
    kind: Literal["dispatch", "result", "feedback", "verdict", "escalation", "system"]
    content: str
    round: int = Field(default=0, ge=0)
    prompt_version: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrchestrationState(TypedDict, total=False):
    """LangGraph state shared across Supervisor and Worker nodes."""

    job_id: str
    workspace_id: str
    brief: Brief
    status: JobStatus
    sub_questions: Annotated[list[str], accumulate]
    evidence: Annotated[list[EvidenceItem], accumulate]
    drafts: Annotated[list[ReportDraft], accumulate]
    transcript: Annotated[list[AgentMessage], accumulate]
    analyst_notes: Annotated[list[AnalystNote], accumulate]
    debate_round: int
    max_debate_rounds: int
    latest_verdict: ReviewVerdict | None
    next_worker: str | None
    final_report: str | None
    errors: Annotated[list[str], accumulate]
    # Orchestration bookkeeping
    team: list[str]  # worker names active for this job's report_type
    dispatches: int  # worker dispatches used so far (supervisor bound)
    mandate: str  # transient: mandate for a dispatched worker (fan-out)
    pending_sends: list[dict[str, str]]  # transient: fan-out dispatches
    cost_so_far: Annotated[float, keep_max]
    budget_usd: float | None  # hard LLM-spend cap; None = unlimited
    # Set by a worker whose LLM call was refused by the pre-call worst-case
    # estimate — the Supervisor escalates instead of dispatching further,
    # then clears it so a funded resume doesn't re-escalate forever.
    budget_blocked: str | None
    escalation_reason: str | None  # "debate_rounds" | "budget"
    # HITL: actions legal for this pause (per hitl.allowed_actions) and the
    # operator's stated reason when a job is rejected.
    allowed_actions: list[str]
    rejection_reason: str | None
    audit_root: str | None
    # Evidence sealing (Phase 6): manifest over gathered sources, sealed
    # into the audit chain before the writer runs.
    evidence_manifest: dict[str, Any] | None
    evidence_root: str | None
    # Prompt provenance: prompt name → version used by each LLM call site.
    prompt_versions: Annotated[dict[str, int], merge_dicts]
