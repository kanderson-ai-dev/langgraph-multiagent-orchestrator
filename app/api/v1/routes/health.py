"""Health endpoints: liveness and readiness.

Readiness reports which optional integrations are configured without leaking
their values — a credential is either present or absent, never echoed.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import Settings, get_settings

router = APIRouter()


class HealthStatus(BaseModel):
    status: str
    version: str


class ReadinessStatus(BaseModel):
    status: str
    version: str
    integrations: dict[str, bool]


@router.get("/health/live", include_in_schema=False)
async def liveness() -> HealthStatus:
    settings = get_settings()
    return HealthStatus(status="ok", version=settings.app_version)


@router.get("/health/ready", include_in_schema=False)
async def readiness() -> ReadinessStatus:
    settings: Settings = get_settings()
    return ReadinessStatus(
        status="ok",
        version=settings.app_version,
        integrations={
            "llm": settings.openai_api_key is not None,
            "search": settings.effective_search_provider != "none",
            "langsmith": settings.langchain_api_key is not None
            and settings.langchain_tracing_v2,
            "auth": settings.auth_enabled,
        },
    )
