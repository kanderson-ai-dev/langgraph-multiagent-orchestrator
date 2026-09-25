"""FastAPI application entrypoint: lifespan, middleware, routers, metrics."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.api.v1.routes.health import router as health_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import instrument_app

logger = get_logger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-XSS-Protection": "0",
}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger.info(
        "app_startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        auth_enabled=settings.auth_enabled,
        search_provider=settings.effective_search_provider,
        llm_configured=settings.openai_api_key is not None,
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
    instrument_app(app)
    return app


app = create_app()
