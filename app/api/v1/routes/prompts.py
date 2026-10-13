"""Prompt lifecycle router — registry browsing, candidates, A/B, promotion.

Prompts are global (not tenant-scoped): a promoted template affects every
workspace's jobs. Mutating endpoints therefore require authentication when
auth is configured — once RBAC lands they become admin-only. Until then any
authenticated caller (or any caller in open local dev) may use them; this
is documented behavior, not an oversight.

Promotion is *metric-gated*: a candidate is evaluated against the promoted
version on the EDD dataset (offline stub path by default) and blocked with
HTTP 409 on any regression, unless an ``override_reason`` (>= 20 chars) is
supplied. Every lifecycle event is appended to the registry's hash-chained
audit log and re-verified on read.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.dependencies import RequestContext, get_context
from app.services.prompt_eval import (
    ABResult,
    EvalCaseLike,
    bounded_eval,
    promotion_gate,
    run_ab_eval,
)
from app.services.prompt_registry import (
    PromptRegistry,
    PromptRegistryError,
    PromptStatus,
)

router = APIRouter(prefix="/prompts", tags=["prompts"])

_AB_RUN_TIMEOUT = 25.0  # seconds — routes never block longer on an eval


# ─── dependencies ────────────────────────────────────────────────────────────


def get_prompt_registry(request: Request) -> PromptRegistry:
    """The app's registry; lazily falls back to an ephemeral in-memory one."""
    reg: PromptRegistry | None = getattr(
        request.app.state, "prompt_registry", None
    )
    if reg is None:
        reg = PromptRegistry.load(db_path=":memory:")
        request.app.state.prompt_registry = reg
    return reg


def _eval_cases() -> list[EvalCaseLike]:
    """The versioned EDD dataset (imported lazily — app never hardcodes it)."""
    try:
        from evaluation.dataset import DATASET
    except ImportError as exc:  # pragma: no cover — deployment without eval pkg
        raise HTTPException(
            status_code=503, detail="evaluation dataset not installed"
        ) from exc
    return list(DATASET)


PromptEvaluator = Callable[[str, int], Awaitable[ABResult]]


def get_prompt_evaluator(
    request: Request,
    settings: Settings = Depends(get_settings),
    registry: PromptRegistry = Depends(get_prompt_registry),
) -> PromptEvaluator:
    """Default evaluator: offline stub-LLM A/B over the EDD dataset.

    Tests override this dependency to inject a stub variant keyed on
    prompt content (a *real* regression produced by the real pipeline).
    """

    async def evaluator(name: str, candidate_version: int) -> ABResult:
        return await run_ab_eval(
            settings, registry, name, candidate_version,
            cases=_eval_cases(), llm=None,
        )

    return evaluator


def _registry_error(exc: PromptRegistryError) -> HTTPException:
    msg = str(exc)
    if "not found" in msg or "unknown prompt name" in msg or "no promoted" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "already promoted" in msg or "only a candidate" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=422, detail=msg)


def _version_json(pv: Any) -> dict[str, Any]:
    return {
        "name": pv.name,
        "version": pv.version,
        "status": str(pv.status),
        "author": pv.author,
        "created_at": pv.created_at.isoformat(),
        "target_model": pv.target_model,
        "variables": pv.variables,
        "changelog": pv.changelog,
        "sha256": pv.sha256,
        "source": pv.source,
    }


# ─── read endpoints ──────────────────────────────────────────────────────────


@router.get("", response_model=dict[str, Any])
async def list_prompts(
    registry: PromptRegistry = Depends(get_prompt_registry),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    return {
        name: [_version_json(pv) for pv in registry.list_versions(name)]
        for name in registry.names()
    }


@router.get("/audit", response_model=dict[str, Any])
async def registry_audit(
    registry: PromptRegistry = Depends(get_prompt_registry),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    """Registry lifecycle audit chain — re-verified on every read."""
    try:
        valid, root, entries = registry.verify_audit_chain()
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    return {"valid": valid, "root": root, "entries": entries}


@router.get("/ab-runs/{run_id}", response_model=dict[str, Any])
async def ab_run_status(
    run_id: str,
    request: Request,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = getattr(request.app.state, "ab_runs", {})
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/{name}", response_model=list[dict[str, Any]])
async def list_versions(
    name: str,
    registry: PromptRegistry = Depends(get_prompt_registry),
    ctx: RequestContext = Depends(get_context),
) -> list[dict[str, Any]]:
    if name not in registry.names():
        raise HTTPException(status_code=404, detail="prompt not found")
    return [_version_json(pv) for pv in registry.list_versions(name)]


@router.get("/{name}/diff", response_model=dict[str, Any])
async def diff_versions(
    name: str,
    request: Request,
    registry: PromptRegistry = Depends(get_prompt_registry),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    from_v = request.query_params.get("from")
    to_v = request.query_params.get("to")
    if not from_v or not to_v:
        raise HTTPException(status_code=422, detail="from and to are required")
    try:
        a, b = int(from_v), int(to_v)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="versions must be integers"
        ) from exc
    try:
        diff = registry.diff(name, a, b)
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    return {"name": name, "from": a, "to": b, "diff": diff}


# ─── candidates ──────────────────────────────────────────────────────────────


class CandidateRequest(BaseModel):
    template: str = Field(min_length=1, max_length=32_000)
    changelog: str = Field(default="", max_length=500)


@router.post("/{name}/candidates", response_model=dict[str, Any], status_code=201)
async def create_candidate(
    name: str,
    body: CandidateRequest,
    registry: PromptRegistry = Depends(get_prompt_registry),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    try:
        pv = registry.create_candidate(
            name, body.template, body.changelog, author=ctx.subject or "api"
        )
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    return _version_json(pv)


# ─── A/B testing ─────────────────────────────────────────────────────────────


class ABRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    live: bool = False  # real LLM instead of the offline stub — job-style


def _latest_candidate(registry: PromptRegistry, name: str) -> int:
    candidates = [
        pv.version
        for pv in registry.list_versions(name)
        if pv.status == PromptStatus.CANDIDATE
    ]
    if not candidates:
        raise HTTPException(status_code=404, detail="no candidate versions")
    return max(candidates)


@router.post("/{name}/ab-test", response_model=dict[str, Any])
async def ab_test(
    name: str,
    body: ABRequest,
    request: Request,
    registry: PromptRegistry = Depends(get_prompt_registry),
    evaluator: PromptEvaluator = Depends(get_prompt_evaluator),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    try:
        registry.get(name)
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    version = body.version or _latest_candidate(registry, name)
    try:
        registry.get(name, version)
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc

    if body.live and settings.openai_api_key is None:
        raise HTTPException(
            status_code=422, detail="live eval requires OPENAI_API_KEY"
        )
    if body.live:
        # Live eval can take minutes — run it job-style, never inline.
        runs = getattr(request.app.state, "ab_runs", None)
        if runs is None:
            runs = {}
            request.app.state.ab_runs = runs
        run_id = uuid.uuid4().hex[:16]
        runs[run_id] = {"status": "running", "name": name, "version": version}

        async def _run() -> None:
            try:
                ab = await evaluator(name, version)
                runs[run_id] = {
                    "status": "done", **ab.model_dump(),
                }
            except Exception as exc:
                runs[run_id] = {"status": "failed", "error": str(exc)}

        asyncio.get_running_loop().create_task(_run())
        return {"run_id": run_id, "status": "running"}

    try:
        ab = await bounded_eval(evaluator(name, version), timeout=_AB_RUN_TIMEOUT)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="eval timed out") from exc
    return ab.model_dump()


# ─── promotion & rollback ────────────────────────────────────────────────────


class PromoteRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    override_reason: str | None = Field(default=None, min_length=20, max_length=500)


class RollbackRequest(BaseModel):
    to_version: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=500)


@router.post("/{name}/promote", response_model=dict[str, Any])
async def promote(
    name: str,
    body: PromoteRequest,
    registry: PromptRegistry = Depends(get_prompt_registry),
    evaluator: PromptEvaluator = Depends(get_prompt_evaluator),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    try:
        registry.get(name)
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    version = body.version or _latest_candidate(registry, name)

    # Cheap validation first — don't burn an eval run on a non-candidate.
    try:
        target = registry.get(name, version)
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    if target.status not in (PromptStatus.CANDIDATE, PromptStatus.ROLLED_BACK):
        raise HTTPException(
            status_code=409, detail="only a candidate can be promoted"
        )

    # Metric gate: offline A/B against the promoted version.
    try:
        ab = await bounded_eval(evaluator(name, version), timeout=_AB_RUN_TIMEOUT)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="eval timed out") from exc
    failures = promotion_gate(ab, epsilon=settings.prompt_promotion_epsilon)
    if failures and not body.override_reason:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "promotion blocked by metric regression",
                "failures": failures,
                "metrics": ab.model_dump(),
            },
        )

    try:
        pv = registry.promote(
            name,
            version,
            actor=ctx.subject or "api",
            reason=body.override_reason or "metrics clean",
            metrics=ab.model_dump(),
            override=bool(body.override_reason),
        )
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    return _version_json(pv)


@router.post("/{name}/rollback", response_model=dict[str, Any])
async def rollback(
    name: str,
    body: RollbackRequest,
    registry: PromptRegistry = Depends(get_prompt_registry),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    try:
        pv = registry.rollback(
            name,
            body.to_version,
            actor=ctx.subject or "api",
            reason=body.reason,
        )
    except PromptRegistryError as exc:
        raise _registry_error(exc) from exc
    return _version_json(pv)
