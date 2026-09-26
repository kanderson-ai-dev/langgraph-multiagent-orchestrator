"""FastAPI application entrypoint: lifespan, middleware, routers, metrics."""

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.dashboard import router as dashboard_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.orchestration import router as orchestration_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import instrument_app
from app.graph.graph import build_graph
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.llm_client import build_llm
from app.services.scraper_client import ScraperClient
from app.services.search_client import build_search_client
from app.services.usage_store import UsageStore

logger = get_logger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-XSS-Protection": "0",
}


def build_runner(
    settings: Settings, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> JobRunner:
    """Construct the runner with a live (checkpointer-backed) graph."""
    llm = build_llm(settings)
    search = build_search_client(settings)
    scraper = ScraperClient(settings)
    graph = build_graph(
        settings, llm=llm, search=search, scraper=scraper,
        checkpointer=checkpointer,
    )
    return JobRunner(
        graph, JobStore(settings.database_path),
        UsageStore(settings.database_path), settings,
    )


def configure_tracing(settings: Settings) -> bool:
    """Bridge LangSmith settings into ``os.environ`` for LangChain tracers.

    ``pydantic-settings`` reads ``.env`` into the ``Settings`` object only —
    LangChain's tracer reads the *process* environment, so the vars must be
    exported before the graph/LLM clients run. Real env vars win (setdefault).
    """
    if not (settings.langchain_tracing_v2 and settings.langchain_api_key):
        return False
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_ENDPOINT", settings.langchain_endpoint)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.langchain_project)
    os.environ.setdefault(
        "LANGCHAIN_API_KEY", settings.langchain_api_key.get_secret_value()
    )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    tracing = configure_tracing(settings)
    # Tests may pre-populate app.state.runner/job_store/usage_store via
    # dependency overrides before the lifespan runs.
    async with AsyncExitStack() as stack:
        if not hasattr(app.state, "runner"):
            # Durable checkpointer: HITL resume survives process restarts.
            from app.services.checkpointer import sqlite_checkpointer

            saver = await stack.enter_async_context(
                sqlite_checkpointer(settings.checkpoint_db_path)
            )
            app.state.runner = build_runner(settings, checkpointer=saver)
            app.state.job_store = app.state.runner.job_store
            app.state.usage_store = app.state.runner.usage_store
        logger.info(
            "app_startup",
            app=settings.app_name,
            version=settings.app_version,
            environment=settings.environment,
            auth_enabled=settings.auth_enabled,
            search_provider=settings.effective_search_provider,
            llm_configured=settings.openai_api_key is not None,
            langsmith_tracing=tracing,
        )
        yield
    logger.info("app_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_and_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    app.include_router(health_router)
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(orchestration_router, prefix=settings.api_prefix)
    app.include_router(dashboard_router, prefix=settings.api_prefix)

    # Frontend: public landing at `/`, operator console at `/console`.
    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.is_dir():
        @app.get("/", include_in_schema=False)
        async def landing() -> FileResponse:
            return FileResponse(frontend / "index.html")

        app.mount(
            "/console",
            StaticFiles(directory=frontend / "console", html=True),
            name="console",
        )

    instrument_app(app)
    return app


app = create_app()
