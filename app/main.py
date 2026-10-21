"""FastAPI application entrypoint: lifespan, middleware, routers, metrics."""

import os
import time
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
from app.api.v1.routes.feedback import router as feedback_router
from app.api.v1.routes.health import api_router as health_api_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.orchestration import router as orchestration_router
from app.api.v1.routes.prompts import router as prompts_router
from app.api.v1.routes.status import config_router
from app.api.v1.routes.status import router as status_router
from app.api.v1.routes.workspaces import router as workspaces_router
from app.core.config import Settings, get_settings
from app.core.event_bus import build_event_bus
from app.core.logging import configure_logging, get_logger
from app.core.metrics import instrument_app
from app.graph.graph import build_graph
from app.services.feedback_store import FeedbackStore
from app.services.job_runner import JobRunner
from app.services.job_store import JobStore
from app.services.llm_client import build_llm
from app.services.prompt_registry import DEFAULT_PROMPTS_DIR, PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import build_search_client
from app.services.tracing import init_tracing, shutdown_tracing
from app.services.usage_store import UsageStore
from app.services.workspace_store import WorkspaceStore

logger = get_logger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-XSS-Protection": "0",
}

# Content-Security-Policy, per surface. The console is fully self-hosted so
# it gets the strictest policy; the landing allows the Tailwind CDN and
# Google Fonts it actually loads; FastAPI's Swagger/ReDoc need jsdelivr;
# everything else is JSON and may not be rendered at all.
_CSP_CONSOLE = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
    " img-src 'self' data:; font-src 'self'; connect-src 'self';"
    " frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_CSP_LANDING = (
    "default-src 'self';"
    " script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com;"
    " style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;"
    " font-src 'self' https://fonts.gstatic.com;"
    " img-src 'self' data:; connect-src 'self';"
    " frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_CSP_DOCS = (
    "default-src 'self';"
    " script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    " style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    " img-src 'self' data: https://fastapi.tiangolo.com;"
    " worker-src 'self' blob:; connect-src 'self';"
    " frame-ancestors 'none'; base-uri 'self'"
)
_CSP_API = "default-src 'none'; frame-ancestors 'none'"


def _csp_for(path: str) -> str:
    if path.startswith("/console"):
        return _CSP_CONSOLE
    if path == "/" or path == "/index.html":
        return _CSP_LANDING
    if path == "/openapi.json" or path.startswith(("/docs", "/redoc")):
        return _CSP_DOCS
    return _CSP_API


def build_runner(
    settings: Settings, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> JobRunner:
    """Construct the runner with a live (checkpointer-backed) graph.

    The Prompt Registry is loaded (and validated) here — a corrupt or
    missing artifact raises and the app fails fast at startup.
    """
    from app.services.cache_store import CacheStore
    from app.services.llm_client import build_llm_overrides

    llm = build_llm(settings)
    cache = CacheStore(settings.database_path) if settings.cache_enabled else None
    search = build_search_client(settings, cache=cache)
    scraper = ScraperClient(settings, cache=cache)
    prompts = PromptRegistry.load(
        DEFAULT_PROMPTS_DIR,
        db_path=settings.database_path,
        signing_key=(
            settings.audit_signing_key.get_secret_value()
            if settings.audit_signing_key
            else None
        ),
    )
    graph = build_graph(
        settings, llm=llm, search=search, scraper=scraper,
        checkpointer=checkpointer, prompts=prompts,
        llm_overrides=build_llm_overrides(settings),
    )
    return JobRunner(
        graph, JobStore(settings.database_path),
        UsageStore(settings.database_path), settings,
        prompt_registry=prompts, scraper=scraper,
        event_bus=build_event_bus(settings),
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
    # Langfuse is opt-in; when it wins the provider slot it disables the
    # LangSmith env flags init_tracing may have just exported.
    tracing_provider = init_tracing(settings)
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
        if not hasattr(app.state, "prompt_registry"):
            app.state.prompt_registry = app.state.runner.prompt_registry
        if not hasattr(app.state, "workspace_store"):
            app.state.workspace_store = WorkspaceStore(settings.database_path)
        if not hasattr(app.state, "feedback_store"):
            app.state.feedback_store = FeedbackStore(settings.database_path)
        if not hasattr(app.state, "token_store"):
            from app.services.token_store import SqliteTokenStore

            app.state.token_store = SqliteTokenStore(settings.database_path)
        app.state.started_at = time.monotonic()
        # Idempotent bootstrap: pre-RBAC implicit tenants land in the
        # ``default`` workspace with the platform admin as its admin member.
        await app.state.workspace_store.migrate(
            admin_subject=settings.admin_username
            if settings.auth_enabled
            else None
        )
        registry = getattr(app.state, "prompt_registry", None)
        if registry is not None:
            registry.publish_metrics()
        # HITL expiry sweeper: deterministic interval loop; sweep logic
        # itself lives in JobRunner.expire_stale (injectable clock).
        import asyncio
        import contextlib

        async def _expiry_loop() -> None:
            while True:
                await asyncio.sleep(settings.hitl_sweep_interval_seconds)
                try:
                    await app.state.runner.expire_stale()
                except Exception:
                    logger.exception("hitl_sweep_failed")

        sweeper = asyncio.create_task(_expiry_loop())
        logger.info(
            "app_startup",
            app=settings.app_name,
            version=settings.app_version,
            environment=settings.environment,
            auth_enabled=settings.auth_enabled,
            search_provider=settings.effective_search_provider,
            llm_configured=settings.openai_api_key is not None,
            langsmith_tracing=tracing,
            tracing_provider=tracing_provider,
            guardrail_llm_screen=settings.guardrail_llm_screen,
        )
        yield
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        with contextlib.suppress(Exception):
            await app.state.runner.aclose()
    shutdown_tracing()
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
        response.headers.setdefault(
            "Content-Security-Policy", _csp_for(request.url.path)
        )
        if request.url.path.startswith("/console"):
            # Console ES-module imports carry no ?v= buster, so heuristic
            # caching can serve stale JS after a deploy — force revalidation.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.include_router(status_router)
    app.include_router(health_router)
    app.include_router(health_api_router, prefix=settings.api_prefix)
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(orchestration_router, prefix=settings.api_prefix)
    app.include_router(dashboard_router, prefix=settings.api_prefix)
    app.include_router(prompts_router, prefix=settings.api_prefix)
    app.include_router(workspaces_router, prefix=settings.api_prefix)
    app.include_router(feedback_router, prefix=settings.api_prefix)
    app.include_router(config_router, prefix=settings.api_prefix)

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
