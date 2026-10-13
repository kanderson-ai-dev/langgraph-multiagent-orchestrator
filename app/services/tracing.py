"""Tracing provider seam — the single module that knows LangSmith/Langfuse.

``TRACING_PROVIDER`` selects the backend:

- ``langsmith``: env-var export for LangChain's built-in tracer
  (``configure_tracing`` in ``app.main`` handles the export itself).
- ``langfuse``: OpenTelemetry-based tracing via the ``langfuse`` SDK —
  active only when BOTH ``LANGFUSE_PUBLIC_KEY`` and
  ``LANGFUSE_SECRET_KEY`` are set, otherwise a silent no-op. When active,
  LangSmith export is disabled so there is no double tracing or quota
  burn.
- ``none`` (default): everything below is a no-op.

The rest of the codebase never imports ``langsmith`` or ``langfuse``
directly: request code wraps graph invocations in ``bind_trace`` and
threads the yielded callbacks into the LangChain ``config``. Any failure
inside the tracing path is logged once at WARNING and never fails or
slows a user request.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Any = None  # lazily-built Langfuse singleton


def _langfuse_configured(settings: Settings) -> bool:
    return (
        settings.tracing_provider == "langfuse"
        and settings.langfuse_public_key is not None
        and settings.langfuse_secret_key is not None
    )


def _mask_payload(*, data: Any, **kwargs: Any) -> Any:
    """Redact secrets and system-prompt text before export.

    Applied by the SDK to every event payload (input/output/metadata):
    - keys matching ``*_key`` / ``*_token`` / ``*_password`` /
      ``authorization`` / ``*_secret`` → ``***REDACTED***``;
    - chat-message content with ``role == "system"`` →
      ``[prompt template masked]`` — prompt text is IP; provenance
      (``prompt_version``/``prompt_hash``) still travels in metadata.
    """
    secret_suffixes = ("_key", "_token", "_password", "_secret")
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for k, v in data.items():
            key = str(k).lower()
            if key.endswith(secret_suffixes) or key == "authorization":
                out[k] = "***REDACTED***"
            elif key == "content" and data.get("role") == "system":
                out[k] = "[prompt template masked]"
            else:
                out[k] = _mask_payload(data=v)
        return out
    if isinstance(data, list):
        return [_mask_payload(data=i) for i in data]
    return data


def _build_client(settings: Settings) -> Any:
    """Build the Langfuse client once; ``None`` when disabled/failed."""
    global _client
    if _client is not None or not _langfuse_configured(settings):
        return _client
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),  # type: ignore[union-attr]
            secret_key=settings.langfuse_secret_key.get_secret_value(),  # type: ignore[union-attr]
            base_url=settings.langfuse_base_url,
            mask=_mask_payload,
        )
    except Exception:
        logger.warning("langfuse_client_init_failed")
        _client = None
    return _client


def init_tracing(settings: Settings) -> str:
    """Activate the configured provider at startup; returns its name.

    Langfuse: builds the client, exports the ``LANGFUSE_*`` env vars the
    SDK reads, runs one ``auth_check`` (logged as a boolean only), and
    disables LangSmith env flags so there is never a double export.
    Never raises — an unreachable backend means "none", not a boot failure.
    """
    if not _langfuse_configured(settings):
        return "none"
    pk = settings.langfuse_public_key.get_secret_value()  # type: ignore[union-attr]
    sk = settings.langfuse_secret_key.get_secret_value()  # type: ignore[union-attr]
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", pk)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", sk)
    os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_base_url)
    # One backend at a time: no silent LangSmith quota burn alongside.
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    client = _build_client(settings)
    if client is None:
        return "none"
    try:
        ok = bool(client.auth_check())
    except Exception:
        logger.warning("langfuse_auth_check_error")
        ok = False
    logger.info("langfuse_auth_check", ok=ok, base_url=settings.langfuse_base_url)
    return "langfuse" if ok else "none"


def shutdown_tracing() -> None:
    """Flush buffered spans then shut down — best-effort, never raises."""
    if _client is None:
        return
    try:
        _client.flush()
        _client.shutdown()
    except Exception:
        logger.warning("langfuse_shutdown_failed")


@contextlib.contextmanager
def bind_trace(
    *,
    name: str,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    trace_id: str | None = None,
    settings: Settings | None = None,
) -> Iterator[list[Any]]:
    """Open a request trace; yields LangChain callbacks for ``config``.

    Inside the context the yielded list contains one fresh
    ``CallbackHandler`` linked to this trace — pass it to
    ``graph.ainvoke(state, config={"callbacks": cb, ...})``. A new
    handler per call means concurrent requests never share a trace.
    Outside Langfuse mode this is a zero-cost ``yield []``.
    """
    client = _client
    if client is None:
        yield []
        return
    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler
        from langfuse.types import TraceContext
    except Exception:
        logger.warning("langfuse_import_failed")
        yield []
        return

    # Inside an existing observation (e.g. a run_experiment item) just
    # attach to the ambient trace — a second root would double-trace.
    try:
        if trace_id is None and client.get_current_trace_id():
            yield [CallbackHandler()]
            return
    except Exception:
        yield []
        return

    # Open root span + trace attributes. Body errors propagate untouched;
    # only setup and teardown failures are swallowed (ExitStack keeps each
    # yield site single — a second yield would break the contextmanager
    # protocol).
    stack = contextlib.ExitStack()
    try:
        ctx: TraceContext | None = {"trace_id": trace_id} if trace_id else None
        root = stack.enter_context(
            client.start_as_current_observation(
                name=name, as_type="agent", trace_context=ctx
            )
        )
        stack.enter_context(
            propagate_attributes(
                session_id=session_id,
                metadata=metadata,
                tags=tags,
                trace_name=name,
            )
        )
        handler = CallbackHandler(
            trace_context={"trace_id": root.trace_id, "parent_span_id": root.id}
        )
    except Exception:
        with contextlib.suppress(Exception):
            stack.close()
        logger.warning("langfuse_trace_failed", trace_name=name)
        yield []
        return
    try:
        yield [handler]
    finally:
        try:
            stack.close()
        except Exception:
            logger.warning("langfuse_trace_close_failed", trace_name=name)


def current_trace_id() -> str | None:
    """Trace id of the active span, if Langfuse is running one."""
    if _client is None:
        return None
    try:
        tid: str | None = _client.get_current_trace_id()
        return tid
    except Exception:
        return None


def create_trace_id() -> str | None:
    """Mint a trace id up-front (for ``X-Trace-Id`` on submit responses)."""
    if _client is None:
        return None
    try:
        tid: str = _client.create_trace_id()
        return tid
    except Exception:
        return None


def reset_client() -> None:
    """Drop the singleton — test isolation only."""
    global _client
    _client = None
