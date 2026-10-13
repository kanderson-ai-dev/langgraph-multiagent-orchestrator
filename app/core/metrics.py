"""Prometheus metrics for the orchestrator.

Counters/histograms are defined here so nodes and routes share one registry;
``instrument_app`` wires the standard HTTP metrics middleware.
"""

from prometheus_client import Counter, Gauge, Histogram
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
PROMPT_VERSION_INFO = Gauge(
    "orchestrator_prompt_version_info",
    "Prompt registry versions (1 = currently promoted)",
    ["name", "version"],
)
LLM_CALLS_TOTAL = Counter(
    "orchestrator_llm_calls_total",
    "LLM calls by agent role and prompt version",
    ["role", "prompt_version"],
)
NODE_SECONDS = Histogram(
    "orchestrator_node_seconds",
    "Wall-clock seconds per graph node execution",
    ["node"],
    buckets=(0.05, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
CACHE_HITS_TOTAL = Counter(
    "orchestrator_cache_hits_total",
    "Search/scrape cache hits and misses, by kind and outcome",
    ["kind", "outcome"],
)


def instrument_app(app: Starlette) -> None:
    """Attach the standard FastAPI HTTP metrics at ``/metrics``."""
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
