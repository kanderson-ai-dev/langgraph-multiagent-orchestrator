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

    # Web search
    search_provider: SearchProvider = "auto"
    search_api_key: SecretStr | None = None
    search_max_results: int = Field(default=5, ge=1, le=20)

    # Scraper politeness & safety
    scrape_delay_seconds: float = Field(default=1.0, ge=0.0)
    scrape_timeout_seconds: float = Field(default=15.0, gt=0.0)
    scrape_max_bytes: int = Field(default=1_048_576, gt=0)
    scrape_respect_robots: bool = True

    # Orchestration bounds
    max_debate_rounds: int = Field(default=3, ge=0, le=10)
    max_sub_questions: int = Field(default=5, ge=1, le=10)

    # LangSmith (optional)
    langchain_tracing_v2: bool = False
    langchain_endpoint: str = "https://api.smith.langchain.com"
    langchain_api_key: SecretStr | None = None
    langchain_project: str = "langgraph-multiagent-orchestrator"

    # Auth (optional — open local dev when unset)
    jwt_secret_key: SecretStr | None = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60, gt=0)
    admin_username: str = "admin"
    admin_password_hash: SecretStr | None = None

    # Rate limiting
    rate_limit_per_minute: int = Field(default=60, ge=0)

    # Persistence
    database_path: str = "data/orchestrator.sqlite"
    checkpoint_db_path: str = "data/checkpoints.sqlite"

    # Cost accounting (USD per 1M tokens)
    cost_input_price_per_1m: float = Field(default=0.15, ge=0.0)
    cost_output_price_per_1m: float = Field(default=0.60, ge=0.0)

    @field_validator(
        "openai_api_key",
        "search_api_key",
        "langchain_api_key",
        "jwt_secret_key",
        "admin_password_hash",
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
