"""Structured logging via structlog.

JSON output outside development; every field whose name matches a sensitive
pattern (``*_key``, ``*_token``, ``*_password``, ``authorization``,
``*_secret``) is redacted before rendering.
"""

import logging
import re
import sys
from typing import Any

import structlog

from app.core.config import Settings

_SENSITIVE_RE = re.compile(r"(.*_key|.*_token|.*_password|authorization|.*_secret)$", re.I)
_REDACTED = "***REDACTED***"


def redact_sensitive_fields(
    _logger: logging.Logger, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: redact sensitive keys anywhere in the event dict."""
    for key in list(event_dict):
        if _SENSITIVE_RE.match(key):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure stdlib + structlog pipelines from settings."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive_fields,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
