"""Feedback router — operator thumbs/comments on reports.

``POST /feedback`` stores a rating per workspace (viewer+ — you must be
able to read the report to rate it). ``GET /feedback`` lists the queue and
``GET /feedback/export`` renders candidate eval-case drafts for manual
review (admin — dataset curation is an admin concern). Exported cases are
reviewed by hand before ever joining the gated dataset.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.dependencies import RequestContext, require_role
from app.services.feedback_store import FeedbackStore

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackIn(BaseModel):
    job_id: str
    rating: Literal["up", "down"]
    comment: str = Field(default="", max_length=2000)
    section: str | None = Field(default=None, max_length=200)


def _store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store  # type: ignore[no-any-return]


@router.post("", status_code=201)
async def submit_feedback(
    body: FeedbackIn,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> dict[str, int]:
    # Generic 404 — never reveal whether the job exists in another tenant.
    job = await request.app.state.job_store.get(ctx.workspace_id, body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    feedback_id = await _store(request).record(
        ctx.workspace_id,
        body.job_id,
        body.rating,
        comment=body.comment,
        section=body.section,
    )
    return {"id": feedback_id}


@router.get("")
async def list_feedback(
    request: Request,
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, Any]:
    items = await _store(request).list(ctx.workspace_id)
    return {"count": len(items), "items": items}


@router.get("/export")
async def export_feedback(
    request: Request,
    ctx: RequestContext = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Candidate eval cases for the feedback queue — manual review only."""
    cases = await _store(request).export_cases(ctx.workspace_id)
    return {"count": len(cases), "cases": cases}
