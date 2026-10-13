"""Application settings via pydantic-settings.

All secrets are ``SecretStr | None`` and empty strings are normalized to
``None`` so an unconfigured CI secret never reads as a real key.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
SearchProvider = Literal["auto", "tavily", "duckduckgo", "none"]


def _empty_to_none(value: object) -> object:
    """Treat ``""`` / whitespace-only strings as ``None`` for secrets."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


class Settings(BaseSettings):
    """Environment-driven configuration. See ``.env.example``."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Application
    app_name: str = "langgraph-multiagent-orchestrator"
    app_version: str = "0.1.0"
    environment: Environment = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # LLM
    openai_api_key: SecretStr | None = None
    chat_model_name: str = "gpt-4o-mini"
    # Optional cheaper/faster model for the Reviewer role; None = same as
    # chat_model_name. Latency candidate (Phase 8).
    reviewer_model: str | None = None
    # Cap output tokens per structured call; None = provider default.
    llm_max_tokens: int | None = Field(default=None, ge=1)
    # LLM-as-judge: deliberately defaults to a *different* model family than
    # the generator so the evaluator isn't the same checkpoint grading itself.
    judge_model: str = "gpt-4o"

    # Web search
    search_provider: SearchProvider = "auto"
    search_api_key: SecretStr | None = None
    search_max_results: int = Field(default=5, ge=1, le=20)

    # Scraper politeness & safety
    scrape_delay_seconds: float = Field(default=1.0, ge=0.0)
    scrape_timeout_seconds: float = Field(default=15.0, gt=0.0)
    scrape_max_bytes: int = Field(default=1_048_576, gt=0)
    scrape_respect_robots: bool = True
    # Bounds total in-flight fetches when callers scrape many URLs in
    # parallel; per-domain politeness still applies on top.
    scrape_max_concurrency: int = Field(default=5, ge=1, le=20)

    # Latency candidates (measured — see docs/eval for the bench harness):
    # only scrape the top-K ranked unique sources per research pass.
    research_scrape_top_k: int = Field(default=8, ge=1, le=50)
    # When the reviewer scores >= this *and* every citation verified, exit
    # the debate early instead of spending another writer round; None off.
    review_early_exit_score: float | None = Field(default=4.5, ge=1.0, le=5.0)
    # Skip the reviewer LLM call entirely when deterministic citation
    # verification already fails below the support threshold.
    reviewer_citation_shortcircuit: bool = True

    # Search/scrape fetch cache (SQLite, keyed workspace_id x key x UTC day)
    cache_enabled: bool = True
    cache_ttl_hours: float = Field(default=24.0, gt=0.0)

    # Orchestration bounds
    max_debate_rounds: int = Field(default=3, ge=0, le=10)
    # HITL: a job paused in awaiting_review for longer than this is swept
    # to `expired` by the background sweeper. 0 disables expiry.
    hitl_expiry_hours: float = Field(default=72.0, ge=0.0)
    hitl_sweep_interval_seconds: float = Field(default=300.0, gt=0.0)
    max_sub_questions: int = Field(default=5, ge=1, le=10)
    # Per-worker dispatch cap in free routing: once a worker has been
    # dispatched this many times it leaves the Supervisor's catalog, so a
    # decider stuck on "gather more evidence" can never loop a worker.
    max_worker_dispatches: int = Field(default=3, ge=1, le=20)

    # LangSmith (optional)
    langchain_tracing_v2: bool = False
    langchain_endpoint: str = "https://api.smith.langchain.com"
    langchain_api_key: SecretStr | None = None
    langchain_project: str = "langgraph-multiagent-orchestrator"

    # Tracing provider: langsmith | langfuse | none.
    # Langfuse is opt-in — active only when BOTH keys are set; a single key
    # or "none" means a silent no-op so tests stay offline. When langfuse is
    # the provider, LangSmith export is disabled (no double tracing).
    tracing_provider: Literal["none", "langsmith", "langfuse"] = "none"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # Auth (optional — open local dev when unset)
    jwt_secret_key: SecretStr | None = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60, gt=0)
    admin_username: str = "admin"
    admin_password_hash: SecretStr | None = None

    # Rate limiting
    rate_limit_per_minute: int = Field(default=60, ge=0)

    # Optional Redis for horizontal-scale backends (rate limiter, SSE
    # event bus). None = single-process in-memory implementations.
    redis_url: SecretStr | None = None

    # Persistence
    database_path: str = "data/orchestrator.sqlite"
    checkpoint_db_path: str = "data/checkpoints.sqlite"

    # Cost accounting (USD per 1M tokens)
    cost_input_price_per_1m: float = Field(default=0.15, ge=0.0)
    cost_output_price_per_1m: float = Field(default=0.60, ge=0.0)
    # Cost governance: default per-job LLM budget; None = unlimited.
    default_job_budget_usd: float | None = Field(default=None, ge=0.0)

    # Prompt lifecycle: max allowed regression vs the promoted version on
    # score-type metrics during promotion gating (safety metrics use 0.0).
    prompt_promotion_epsilon: float = Field(default=0.05, ge=0.0)

    @field_validator(
        "openai_api_key",
        "search_api_key",
        "langchain_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        "jwt_secret_key",
        "admin_password_hash",
        "redis_url",
        mode="before",
    )
    @classmethod
    def _secret_empty_to_none(cls, value: object) -> object:
        return _empty_to_none(value)

    @property
    def auth_enabled(self) -> bool:
        """JWT auth is enforced only when a secret and admin credentials exist."""
        return self.jwt_secret_key is not None and self.admin_password_hash is not None

    @property
    def effective_search_provider(self) -> Literal["tavily", "duckduckgo", "none"]:
        """Resolve ``auto`` to a concrete provider."""
        if self.search_provider == "auto":
            return "tavily" if self.search_api_key is not None else "duckduckgo"
        return self.search_provider


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton; tests may override via dependency injection."""
    return Settings()
