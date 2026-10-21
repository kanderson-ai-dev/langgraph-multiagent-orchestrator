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
from app.core.dependencies import RequestContext, get_rate_limiter, require_role
from app.core.rate_limit import RateLimiter
from app.graph.state import Brief
from app.services.job_runner import JobRunner
from app.services.job_store import JobRecord
from app.services.store_protocols import JobStoreProtocol
from app.services.tracing import create_trace_id

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


def _jobs(request: Request) -> JobStoreProtocol:
    return request.app.state.job_store  # type: ignore[no-any-return]


@router.post("/reports", response_model=JobResponse, status_code=202)
async def submit(
    body: SubmitRequest,
    request: Request,
    response: Response,
    ctx: RequestContext = Depends(require_role("member")),
    settings: Settings = Depends(get_settings),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> JobResponse:
    client = ctx.subject or (request.client.host if request.client else "anon")
    if not await limiter.allow(f"submit:{client}"):
        raise HTTPException(status_code=429, detail="too many requests")

    try:
        brief = Brief.model_validate(body.model_dump())
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid brief") from None
    job = await _jobs(request).create(ctx.workspace_id, brief)
    # Mint the trace id up-front so the header names the trace the
    # background run will land in (no-op when tracing is off).
    trace_id = create_trace_id()
    await _runner(request).submit(
        job.job_id, ctx.workspace_id, brief, trace_id=trace_id
    )
    if trace_id:
        response.headers["X-Trace-Id"] = trace_id
    return _record_to_response(job)


@router.get("/reports", response_model=list[dict[str, Any]])
async def list_jobs(
    request: Request,
    limit: int = 20,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> list[dict[str, Any]]:
    """Recent jobs for this workspace, newest first (lightweight summaries)."""
    jobs = await _jobs(request).list(ctx.workspace_id, limit=min(limit, 100))
    return [
        {
            "job_id": j.job_id,
            "topic": j.brief.topic,
            "report_type": j.brief.report_type,
            "status": j.status,
            "cost_usd": j.cost_usd,
            "created_at": j.created_at.isoformat(),
            "updated_at": j.updated_at.isoformat(),
        }
        for j in jobs
    ]


@router.get("/reports/{job_id}", response_model=dict[str, Any])
async def poll(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> dict[str, Any]:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    from app.graph.teams import team_for

    return {
        "job_id": job.job_id,
        "status": job.status,
        "cost_usd": job.cost_usd,
        "brief": job.brief.model_dump(),
        "team": team_for(job.brief.report_type),
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@router.get("/reports/{job_id}/stream")
async def stream(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    # Re-attach support: a client that saw events up to `id: n` sends
    # Last-Event-ID: n and gets only what it missed. A non-integer or
    # negative value is ignored — never a 4xx on a resume hint.
    last_id: int | None = None
    raw_last = request.headers.get("last-event-id")
    if raw_last is not None:
        try:
            parsed = int(raw_last)
            last_id = parsed if parsed >= 0 else None
        except ValueError:
            last_id = None

    async def events() -> AsyncIterator[str]:
        """SSE stream: heartbeat comments keep proxies alive, client
        disconnect frees the bus subscriber, and a hard lifetime cap ends
        stale streams with a ``timeout`` frame the console re-attaches on.
        Job events carry ``id: <seq>`` so re-attaches can resume with
        ``Last-Event-ID`` instead of re-reading the whole history."""
        import asyncio
        import contextlib
        import time

        source = _runner(request).stream(job_id, after=last_id)
        it = source.__aiter__()
        deadline = time.monotonic() + settings.sse_max_stream_seconds
        # The pending ``anext`` must never be cancelled — cancelling it
        # would close the upstream generator (and the bus subscription).
        # asyncio.wait + keeping the task across ticks leaves it running.
        pending: asyncio.Task[dict[str, Any]] | None = None
        try:
            while True:
                # is_disconnected() awaits a message on the receive
                # channel — it can block under some ASGI transports, so
                # it gets a tiny bounded wait; the unconditional safety
                # net is the server cancelling this task on disconnect.
                try:
                    disconnected = await asyncio.wait_for(
                        request.is_disconnected(), timeout=0.05
                    )
                except TimeoutError:
                    disconnected = False
                if disconnected:
                    break
                if time.monotonic() >= deadline:
                    yield f"data: {json.dumps({'type': 'timeout'})}\n\n"
                    break
                if pending is None:
                    pending = asyncio.ensure_future(anext(it))
                done, _ = await asyncio.wait(
                    {pending}, timeout=settings.sse_heartbeat_seconds
                )
                if not done:
                    yield ": ping\n\n"
                    continue
                try:
                    e = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                seq = e.get("seq")
                if seq is not None:
                    yield f"id: {seq}\ndata: {json.dumps(e)}\n\n"
                else:
                    yield f"data: {json.dumps(e)}\n\n"
        finally:
            if pending is not None and not pending.done():
                # Cancel, then await — aclose() raises "generator already
                # running" while a cancelled anext is still in flight.
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
            # Closing the generator runs the bus's own finally → the
            # subscriber queue is removed even on abrupt disconnects.
            await source.aclose()

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/reports/{job_id}/review", response_model=dict[str, str])
async def review(
    job_id: str,
    body: ReviewRequest,
    request: Request,
    ctx: RequestContext = Depends(require_role("member")),
) -> dict[str, str]:
    from app.graph.hitl import ALL_ACTIONS, approve_hint

    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"job is {job.status}, not awaiting_review")
    if body.action not in ALL_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {'|'.join(ALL_ACTIONS)}",
        )
    result = job.result or {}
    allowed = result.get("allowed_actions")
    if isinstance(allowed, list) and body.action not in allowed:
        if body.action == "approve" and not result.get("has_draft"):
            raise HTTPException(
                status_code=422,
                detail=approve_hint(
                    str(result.get("escalation_reason") or "")
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=(
                f"action '{body.action}' is not allowed for this escalation "
                f"({result.get('escalation_reason') or 'unknown'}); allowed: "
                f"{', '.join(str(a) for a in allowed)}"
            ),
        )
    if body.action in ("fund", "fund_and_continue") and (
        body.additional_budget_usd <= 0.0
    ):
        raise HTTPException(
            status_code=422,
            detail="additional_budget_usd must be > 0 to fund the job",
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
    ctx: RequestContext = Depends(require_role("viewer")),
) -> Response:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    from app.services.pdf_export import markdown_to_pdf

    pdf = markdown_to_pdf(
        _report_md(job), title=job.brief.topic, footer=_report_footer(job)
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{job_id}.pdf"'},
    )


def _report_md(job: Any) -> str:
    if not job.result or not job.result.get("report"):
        raise HTTPException(status_code=409, detail="report not ready")
    return str(job.result["report"])


def _report_footer(job: Any) -> str:
    parts: list[str] = []
    raw = job.result.get("prompt_versions")
    if isinstance(raw, dict) and raw:
        rendered = " · ".join(
            f"{n.replace('_', ' ').title()} v{v}"
            for n, v in sorted(raw.items())
        )
        parts.append(f"Prompt versions: {rendered}")
    if job.result.get("audit_root"):
        sig = job.result.get("audit_signature")
        if isinstance(sig, str) and sig:
            from app.services.audit import signature_key_id

            kid = signature_key_id(sig)
            parts.append(f"Audit: signed{f' (key {kid})' if kid else ''}")
        else:
            parts.append("Audit: unsigned")
    return " — ".join(parts)


@router.get("/reports/{job_id}/report.md")
async def report_md(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> Response:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    md = _report_md(job)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="report-{job_id}.md"'
        },
    )


@router.get("/reports/{job_id}/report.docx")
async def report_docx(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> Response:
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    from app.services.docx_export import markdown_to_docx

    docx = markdown_to_docx(
        _report_md(job), title=job.brief.topic, footer=_report_footer(job)
    )
    return Response(
        content=docx,
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="report-{job_id}.docx"'
        },
    )


@router.get("/reports/{job_id}/audit", response_model=dict[str, Any])
async def audit_trail(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the hash-chained audit trail and re-verify it on the fly.

    ``valid`` is recomputed from the stored transcript — a tampered history
    would fail verification rather than parrot a stored ``True``. When the
    deployment holds ``AUDIT_SIGNING_KEY``, the stored ``audit_signature``
    is verified too: a DB-only attacker could recompute a consistent chain
    but could not mint the HMAC. Unsigned deployments report
    ``signature_valid: null`` honestly.
    """
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.result or "transcript" not in job.result:
        raise HTTPException(status_code=409, detail="audit trail not ready")

    from app.graph.state import AgentMessage
    from app.services.audit import (
        GENESIS,
        chain_transcript,
        signature_key_id,
        signing_keyring,
        verify_chain,
        verify_signature,
    )
    from app.services.audit_anchor import check_anchor

    raw = job.result["transcript"]
    if not isinstance(raw, list):
        raise HTTPException(status_code=409, detail="audit trail not ready")
    messages = [AgentMessage.model_validate(m) for m in raw]
    entries = chain_transcript(messages)
    stored_root = str(job.result.get("audit_root") or GENESIS)
    signature = job.result.get("audit_signature")
    signature_valid: bool | None = None
    keyring = signing_keyring(settings)
    if keyring and isinstance(signature, str) and signature:
        signature_valid = verify_signature(
            keyring,
            signature,
            stored_root,
            job_id=job.job_id,
            workspace_id=job.workspace_id,
        )
    return {
        "job_id": job.job_id,
        "audit_root": stored_root,
        "valid": verify_chain(messages, stored_root),
        "signed": signature_valid is not None,
        "signature_valid": signature_valid,
        "signature_key_id": signature_key_id(
            signature if isinstance(signature, str) else None
        ),
        "anchor_match": check_anchor(
            settings.audit_anchor_path,
            job_id=job.job_id,
            audit_root=stored_root,
        ),
        "entries": [e.model_dump() for e in entries],
        "evidence_root": job.result.get("evidence_root"),
        "evidence_manifest": job.result.get("evidence_manifest"),
    }


@router.get("/reports/{job_id}/evidence", response_model=dict[str, Any])
async def evidence(
    job_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> dict[str, Any]:
    """List the job's sealed evidence sources with their content hashes.

    The manifest is sealed into the audit chain before the writer runs —
    this endpoint exposes the same sealed view the report's citation
    footnotes reference by short hash.
    """
    job = await _jobs(request).get(ctx.workspace_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    result = job.result or {}
    manifest = result.get("evidence_manifest")
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=409, detail="evidence not sealed yet")
    return {
        "job_id": job.job_id,
        "evidence_root": result.get("evidence_root"),
        "count": manifest.get("count", 0),
        "items": manifest.get("items", []),
    }
