"""Dashboard router — per-workspace cost/latency summary."""

from fastapi import APIRouter, Depends, Request

from app.core.dependencies import RequestContext, get_context
from app.services.usage_store import UsageStore

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def summary(
    request: Request,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, object]:
    store: UsageStore = request.app.state.usage_store
    s = await store.workspace_summary(ctx.workspace_id)
    return {
        "workspace_id": ctx.workspace_id,
        "total_cost_usd": s.total_cost_usd,
        "total_prompt_tokens": s.total_prompt_tokens,
        "total_completion_tokens": s.total_completion_tokens,
        "avg_latency_ms": s.avg_latency_ms,
        "llm_calls": s.event_count,
        "cost_by_role": s.by_role,
    }
