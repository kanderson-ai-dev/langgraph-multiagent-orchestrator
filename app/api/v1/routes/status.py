"""Public status surfaces — payload-free.

``/status`` reports mode, version, uptime and aggregate job counts (no
tenant-identifying data). ``/eval/latest`` serves the committed EDD
scorecard so the public eval state is inspectable without auth.
``/config/ui`` serves the white-label configuration the frontends apply
before any login.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.services.store_protocols import JobStoreProtocol

router = APIRouter(tags=["status"])

_ROOT = Path(__file__).resolve().parents[4]
_SCORECARD = _ROOT / "evaluation" / "results" / "scorecard.json"


@router.get("/status", include_in_schema=False)
async def status(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    started = getattr(request.app.state, "started_at", None)
    uptime = round(time.monotonic() - started, 1) if started else None
    jobs: JobStoreProtocol = request.app.state.job_store
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "auth_enabled": settings.auth_enabled,
        "uptime_seconds": uptime,
        "jobs": await jobs.status_counts_all(),
    }


@router.get("/eval/latest", include_in_schema=False)
async def eval_latest() -> JSONResponse:
    """The committed scorecard — same artifact the README badge cites."""
    if not _SCORECARD.is_file():
        return JSONResponse({"detail": "no scorecard committed"}, status_code=404)
    try:
        data = json.loads(_SCORECARD.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return JSONResponse({"detail": "scorecard unreadable"}, status_code=500)
    return JSONResponse(
        {
            "generated_at": data.get("generated_at"),
            "model": data.get("model"),
            "judge_model": data.get("judge_model"),
            "mode": data.get("mode"),
            "note": data.get("note"),
            "metrics": data.get("metrics"),
            "gates": data.get("gates"),
            "thresholds": data.get("thresholds"),
        }
    )


config_router = APIRouter(tags=["config"])


@config_router.get("/config/ui")
async def ui_config(
    settings: Settings = Depends(get_settings),
) -> dict[str, str | None]:
    """White-label config — public so the landing can brand pre-login."""
    return {
        "product_name": settings.ui_product_name,
        "accent_color": settings.ui_accent_color,
        "logo_url": settings.ui_logo_url,
    }
