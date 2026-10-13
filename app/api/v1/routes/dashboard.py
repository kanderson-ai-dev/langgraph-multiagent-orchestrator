"""Dashboard router — per-workspace cost/latency summary."""

from fastapi import APIRouter, Depends, Request

from app.core.dependencies import RequestContext, require_role
from app.services.usage_store import UsageStore

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def summary(
    request: Request,
    ctx: RequestContext = Depends(require_role("viewer")),
) -> dict[str, object]:
    from app.core.latency import LATENCY

    store: UsageStore = request.app.state.usage_store
    s = await store.workspace_summary(ctx.workspace_id)
    quantiles = LATENCY.quantiles()
    job = quantiles.pop("job", {})
    return {
        "workspace_id": ctx.workspace_id,
        "total_cost_usd": s.total_cost_usd,
        "total_prompt_tokens": s.total_prompt_tokens,
        "total_completion_tokens": s.total_completion_tokens,
        "avg_latency_ms": s.avg_latency_ms,
        "llm_calls": s.event_count,
        "cost_by_role": s.by_role,
        # Per-node p50/p95 wall seconds (rolling window) + job-segment latency.
        "node_latency_seconds": quantiles,
        "job_latency_seconds": job,
    }
