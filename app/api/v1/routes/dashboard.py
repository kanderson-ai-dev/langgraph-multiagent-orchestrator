"""Dashboard router — per-workspace cost/latency summary + performance."""

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings, get_settings
from app.core.dependencies import RequestContext, require_role
from app.services.job_store import JobStore
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


@router.get("/performance")
async def performance(
    request: Request,
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(require_role("viewer")),
) -> dict[str, Any]:
    """Cost & performance product surface for one tenant.

    Every number is measured — from the job store (status, budget,
    escalation reasons, daily jobs), the usage store (per-role cost /
    latency / prompt-version mix) and the in-process latency tracker
    (node + job p50/p95). Nothing is estimated or hardcoded.
    """
    from app.core.cost_tracking import PRICE_TABLE_VERSION
    from app.core.latency import LATENCY

    ws = ctx.workspace_id
    jobs: JobStore = request.app.state.job_store
    usage: UsageStore = request.app.state.usage_store

    counts = await jobs.status_counts(ws)
    done = counts.get("done", 0)
    finished = sum(
        counts.get(s, 0)
        for s in ("done", "failed", "blocked", "expired", "awaiting_review")
    )
    total = sum(counts.values())
    escalated = counts.get("awaiting_review", 0) + counts.get("expired", 0)

    summary = await usage.workspace_summary(ws)
    by_role = await usage.per_role_stats(ws)
    prompt_mix = await usage.prompt_version_mix(ws)
    daily_jobs = await jobs.daily_stats(ws)
    daily_cost = await usage.daily_cost(ws)
    for row in daily_jobs:
        row["cost_usd"] = daily_cost.get(str(row["date"]), 0.0)
    budget = await jobs.budget_usage(ws)
    escalations = await jobs.escalation_reasons(ws)

    quantiles = LATENCY.quantiles()
    job_lat = quantiles.pop("job", {})

    human = None
    if done:
        human_cost = done * settings.human_hours_per_report * settings.human_hourly_usd
        human = {
            "reports_completed": done,
            "hours_per_report": settings.human_hours_per_report,
            "hourly_usd": settings.human_hourly_usd,
            "human_cost_total_usd": round(human_cost, 4),
            "agent_cost_total_usd": round(summary.total_cost_usd, 6),
            "savings_usd": round(human_cost - summary.total_cost_usd, 4),
            "cost_per_report": {
                "human_usd": round(
                    settings.human_hours_per_report * settings.human_hourly_usd, 2
                ),
                "agent_usd": round(summary.total_cost_usd / done, 6),
            },
        }

    return {
        "workspace_id": ws,
        "price_table_version": PRICE_TABLE_VERSION,
        "totals": {"jobs": total, "by_status": counts},
        "success_rate": round(done / finished, 4) if finished else None,
        "convergence_rate": round(done / finished, 4) if finished else None,
        "escalation_rate": round(escalated / total, 4) if total else None,
        "escalation_reasons": escalations,
        "cost": {
            "total_usd": summary.total_cost_usd,
            "avg_per_job_usd": (
                round(summary.total_cost_usd / total, 6) if total else None
            ),
            "by_role": by_role,
        },
        "latency_seconds": {"job": job_lat, "nodes": quantiles},
        "daily": daily_jobs,
        "budget": {
            **budget,
            "pct_used": (
                round(budget["spent_usd"] / budget["budget_usd"] * 100, 2)
                if budget["budget_usd"]
                else None
            ),
        },
        "prompt_version_mix": prompt_mix,
        # Explicit empty state — the UI must not invent a comparison.
        "human_vs_agent": human,
    }
