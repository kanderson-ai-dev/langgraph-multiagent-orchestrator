"""Orchestration router — submit/poll/stream/review/download report jobs.

All endpoints are scoped by ``workspace_id`` (multi-tenant isolation comes
from ``RequestContext`` — a job in workspace A is invisible to workspace B).
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings, get_settings
from app.core.dependencies import RequestContext, get_context, get_rate_limiter
from app.core.rate_limit import RateLimiter
from app.graph.state import Brief
from app.services.job_runner import JobRunner
from app.services.job_store import JobRecord, JobStore

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


class SubmitRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    audience: str = "general business"
    tone: str = "analytical"
    requirements: list[str] = []
    source_urls: list[str] = []
    language: str = "en"
    report_type: str = "general"
    tenant_context: str = ""
    budget_usd: float | None = Field(default=None, ge=0.0)


class ReviewRequest(BaseModel):
    action: str  # approve | edit | reject | fund
    feedback: str = ""
    additional_budget_usd: float = Field(default=0.0, ge=0.0)


class JobResponse(BaseModel):
    job_id: str
    workspace_id: str
    status: str
    cost_usd: float


def _record_to_response(r: JobRecord) -> JobResponse:
    return JobResponse(
        job_id=r.job_id, workspace_id=r.workspace_id,
        status=r.status, cost_usd=r.cost_usd,
    )


def _runner(request: Request) -> JobRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


def _jobs(request: Request) -> JobStore:
    return request.app.state.job_store  # type: ignore[no-any-return]


@router.post("/reports", response_model=JobResponse, status_code=202)
async def submit(
    body: SubmitRequest,
    request: Request,
    ctx: RequestContext = Depends(get_context),
    settings: Settings = Depends(get_settings),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> JobResponse:
    client = ctx.subject or (request.client.host if request.client else "anon")
    if not limiter.allow(f"submit:{client}"):
        raise HTTPException(status_code=429, detail="too many requests")

    try:
        brief = Brief.model_validate(body.model_dump())
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid brief") from None
    job = await _jobs(request).create(ctx.workspace_id, brief)
    await _runner(request).submit(job.job_id, ctx.workspace_id, brief)
    return _record_to_response(job)


@router.get("/reports/{job_id}", response_model=dict[str, Any])
async def poll(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "cost_usd": job.cost_usd,
        "brief": job.brief.model_dump(),
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@router.get("/reports/{job_id}/stream")
async def stream(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(get_context),
) -> StreamingResponse:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def events() -> AsyncIterator[str]:
        async for e in _runner(request).stream(job_id):
            yield f"data: {json.dumps(e)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/reports/{job_id}/review", response_model=dict[str, str])
async def review(
    job_id: str,
    body: ReviewRequest,
    request: Request,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, str]:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not awaiting_review")
    if body.action not in ("approve", "edit", "reject", "fund"):
        raise HTTPException(
            status_code=422, detail="action must be approve|edit|reject|fund"
        )
    await _runner(request).resume(
        job_id,
        ctx.workspace_id,
        {
            "action": body.action,
            "feedback": body.feedback,
            "additional_budget_usd": body.additional_budget_usd,
        },
    )
    return {"status": "resumed"}


@router.get("/reports/{job_id}/report.pdf")
async def report_pdf(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(get_context),
) -> Response:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.result or not job.result.get("report"):
        raise HTTPException(status_code=409, detail="report not ready")

    from app.services.pdf_export import markdown_to_pdf

    pdf = markdown_to_pdf(str(job.result["report"]), title=job.brief.topic)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{job_id}.pdf"'},
    )
