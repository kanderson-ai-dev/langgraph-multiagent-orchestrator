"""Prometheus metrics for the orchestrator.

Counters/histograms are defined here so nodes and routes share one registry;
``instrument_app`` wires the standard HTTP metrics middleware.
"""

from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.applications import Starlette

JOBS_TOTAL = Counter(
    "orchestrator_jobs_total",
    "Total orchestration jobs by terminal status",
    ["status"],
)
JOB_DURATION_SECONDS = Histogram(
    "orchestrator_job_duration_seconds",
    "End-to-end orchestration job latency (excluding HITL pauses)",
    buckets=(5, 15, 30, 60, 90, 120, 180, 240, 300),
)
LLM_TOKENS_TOTAL = Counter(
    "orchestrator_llm_tokens_total",
    "LLM tokens consumed, by agent role and token kind",
    ["role", "kind"],
)
LLM_COST_USD = Counter(
    "orchestrator_llm_cost_usd_total",
    "Estimated LLM cost in USD, by agent role",
    ["role"],
)
DEBATE_ROUNDS_TOTAL = Counter(
    "orchestrator_debate_rounds_total",
    "Writer-Reviewer debate rounds executed",
)
HUMAN_REVIEW_TOTAL = Counter(
    "orchestrator_human_review_total",
    "Jobs escalated to human review, by resolution",
    ["resolution"],
)
BLOCKED_REQUESTS_TOTAL = Counter(
    "orchestrator_blocked_requests_total",
    "Requests rejected by the input guardrail",
)
BUDGET_EXCEEDED_TOTAL = Counter(
    "orchestrator_budget_exceeded_total",
    "Jobs escalated to human review because the LLM budget was exhausted",
)


def instrument_app(app: Starlette) -> None:
    """Attach the standard FastAPI HTTP metrics at ``/metrics``."""
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
