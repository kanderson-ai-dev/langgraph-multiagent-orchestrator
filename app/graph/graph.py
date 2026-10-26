"""Graph assembly — the Supervisor-Workers orchestration pipeline.

Topology:

    START -> input_guardrail -> supervisor_router
    supervisor_router -> researcher | writer | reviewer | human_review
                      | report_assembler | END
    researcher/writer/reviewer -> supervisor_router   (the debate loop)
    human_review -> END  (Phase 7 wires interrupt/resume)
    report_assembler -> END

The Supervisor's LLM decider routes freely while no verdict is pending;
once the Reviewer issues a verdict the bound is enforced deterministically
via ``decide_next_after_verdict`` — termination is structural, not hoped-for.
"""

import time
from collections.abc import Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.config import Settings
from app.framework import (
    FINISH,
    DispatchSpec,
    RouterDecider,
    Supervisor,
    Worker,
    WorkerRegistry,
    decide_next_after_verdict,
)
from app.graph.deciders import make_llm_decider, rule_based_decider
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput, Rubric
from app.graph.nodes.specialist import Finding, SpecialistAnalysis
from app.graph.nodes.writer import DraftOutput
from app.graph.report_assembler import assemble_report
from app.graph.state import AgentMessage, OrchestrationState
from app.graph.teams import build_all_workers, team_for
from app.services.llm_client import StructuredLLM, StubLLM
from app.services.prompt_registry import DEFAULT_PROMPTS_DIR, PromptRegistry
from app.services.scraper_client import ScraperClient
from app.services.search_client import SearchClient


def _supervisor_targets(registry: WorkerRegistry) -> dict[Hashable, str]:
    """Conditional-edge map: Supervisor output names → node names."""
    mapping: dict[Hashable, str] = {name: name for name in registry.names()}
    mapping[FINISH] = "report_assembler"
    mapping["human_review"] = "human_review"
    return mapping


def _as_node(worker: Worker) -> Any:
    """Adapt a ``Worker`` to a LangGraph node callable over TypedDict state."""

    async def node(state: OrchestrationState) -> dict[str, Any]:
        try:
            return await worker.run(dict(state))
        except Exception as exc:
            # A pre-call budget refusal must escalate, never crash the job —
            # the Supervisor converts `budget_blocked` into a human_review
            # pause with the same reason the between-call gate uses.
            from app.core.cost_tracking import BudgetExceededError

            if not isinstance(exc, BudgetExceededError):
                raise
            return {
                "budget_blocked": str(exc),
                "transcript": [
                    AgentMessage(
                        sender=worker.name, recipient="supervisor",
                        kind="system",
                        content=f"LLM call refused by budget gate: {exc}",
                    )
                ],
            }

    return _timed(worker.name, node)


def _timed(name: str, fn: Any) -> Any:
    """Wrap a node callable with per-node wall-time instrumentation.

    Feeds both the Prometheus ``orchestrator_node_seconds`` histogram and
    the in-process quantile tracker the dashboard reads for p50/p95.
    """

    async def wrapped(state: OrchestrationState) -> dict[str, Any]:
        start = time.monotonic()
        try:
            result: dict[str, Any] = await fn(state)
            return result
        finally:
            seconds = time.monotonic() - start
            from app.core.latency import LATENCY
            from app.core.metrics import NODE_SECONDS

            NODE_SECONDS.labels(node=name).observe(seconds)
            LATENCY.observe(name, seconds)

    return wrapped


def _register_stub_builders(llm: StubLLM) -> None:
    """Give the offline stub deterministic, schema-valid outputs per role."""
    from app.graph.nodes.writer import DraftCitation

    llm.register_default(
        "ResearchPlan",
        lambda s, c: ResearchPlan(
            sub_questions=["What does the topic require?"],
            queries=[],
        ),
    )
    llm.register_default(
        "DraftOutput",
        lambda s, c: DraftOutput(
            title="Stub Report",
            markdown="## Summary\nOffline stub draft (no LLM credentials configured).",
            citations=[
                DraftCitation(claim="stub claim", quote="stub quote", source_url="local")
            ],
        ),
    )
    llm.register_default(
        "ReviewOutput",
        lambda s, c: ReviewOutput(
            verdict="approve",
            score=4.0,
            rubric=Rubric(structure=4.0, clarity=4.0, grounding=4.0, tone=4.0),
            feedback=[],
        ),
    )
    llm.register_default(
        "SpecialistAnalysis",
        lambda s, c: SpecialistAnalysis(
            findings=[Finding(text="offline stub finding")],
            flags=[],
            recommendation="offline stub recommendation",
        ),
    )


def build_graph(
    settings: Settings,
    *,
    llm: StructuredLLM,
    search: SearchClient,
    scraper: ScraperClient,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    decider: RouterDecider | None = None,
    prompts: PromptRegistry | None = None,
    llm_overrides: dict[str, StructuredLLM] | None = None,
) -> CompiledStateGraph[Any]:
    """Compile the orchestration graph. Injectable deps keep it testable.

    ``prompts`` is the Prompt Registry every agent renders its system
    prompt through; when omitted, the YAML seeds under ``prompts/`` are
    loaded (fails fast on any invalid artifact).
    """

    prompts = prompts or PromptRegistry.load(DEFAULT_PROMPTS_DIR)
    registry = WorkerRegistry()
    # Every known worker is a node; each job's `team` (from report_type)
    # narrows what the Supervisor may route to.
    for worker in build_all_workers(
        llm=llm, search=search, scraper=scraper, settings=settings,
        prompts=prompts, llm_overrides=llm_overrides,
    ):
        registry.register(worker)
    writer = registry.get("writer")
    reviewer = registry.get("reviewer")

    if isinstance(llm, StubLLM):
        _register_stub_builders(llm)

    using_llm_decider = decider is None and not isinstance(llm, StubLLM)
    if decider is None:
        decider = (
            rule_based_decider
            if isinstance(llm, StubLLM)
            else make_llm_decider(llm, prompts)
        )
    supervisor = Supervisor(
        registry, decider, max_dispatches=settings.supervisor_max_dispatches
    )

    async def input_guardrail_node(state: OrchestrationState) -> dict[str, Any]:
        """Screen the brief before any paid call — blocked input burns 0 tokens."""
        from app.graph.guardrails import brief_fields, llm_screen_brief, screen_brief

        brief = state["brief"]
        result = screen_brief(brief)
        if not result.allowed:
            return {
                "status": "blocked",
                "transcript": [
                    AgentMessage(
                        sender="input_guardrail", recipient="user",
                        kind="system", content=f"Request blocked: {result.reason}",
                    )
                ],
            }
        # Optional probabilistic second layer — only after the deterministic
        # regex pass, only on a real LLM (never the stub), tracked as a
        # "guardrail"-role call. One paid call per *accepted* brief. When the
        # pre-call budget estimate refuses it, the optional screen degrades
        # to "skipped" — the deterministic layer already ran.
        screened = False
        if settings.guardrail_llm_screen:
            from app.core.cost_tracking import BudgetExceededError

            try:
                screened = await llm_screen_brief(llm, brief_fields(brief))
            except BudgetExceededError:
                screened = False
        if screened:
            return {
                "status": "blocked",
                "transcript": [
                    AgentMessage(
                        sender="input_guardrail", recipient="user",
                        kind="system",
                        content="Request blocked: flagged by screening layer",
                    )
                ],
            }
        return {
            "status": "running",
            "transcript": [
                AgentMessage(
                    sender="input_guardrail", recipient="supervisor",
                    kind="system", content="Brief accepted.",
                )
            ],
        }

    async def supervisor_node(state: OrchestrationState) -> dict[str, Any]:
        verdict = state.get("latest_verdict")
        rounds = state.get("debate_round", 0)
        max_rounds = state.get("max_debate_rounds", settings.max_debate_rounds)
        decided_via_llm = False

        # Cost governance: a spent budget stops all further paid work before
        # the decider itself burns tokens. An approved draft may still finish.
        # `budget_blocked` arrives from a worker whose call was refused by
        # the pre-call worst-case estimate — same escalation, same reason.
        budget = state.get("budget_usd")
        refusal = state.get("budget_blocked") or ""
        over_budget = budget is not None and state.get("cost_so_far", 0.0) >= budget
        if (over_budget or refusal) and not (
            verdict and verdict.verdict == "approve"
        ):
            from app.core.metrics import BUDGET_EXCEEDED_TOTAL

            BUDGET_EXCEEDED_TOTAL.inc()
            nxt, reason = "human_review", "budget_exceeded"
        elif verdict is not None:
            drafts = state.get("drafts", [])
            has_newer_draft = drafts and drafts[-1].version > verdict.draft_version
            if verdict.verdict == "revise" and has_newer_draft:
                # Writer already produced a newer draft — send it for review.
                nxt = reviewer.name
                reason = f"new draft v{drafts[-1].version} pending review"
            else:
                # Debate bound is deterministic — never left to the decider.
                # A `revise` verdict always returns to the Writer while rounds
                # remain: semantic claim adjudication means a high-scoring
                # revise still names a concrete, fixable deficiency, so the
                # old "early exit on clean citations" shortcut only shipped
                # non-converged drafts the debate could have repaired.
                nxt = decide_next_after_verdict(
                    approved=verdict.verdict == "approve",
                    rounds_used=rounds,
                    max_rounds=max_rounds,
                    proposer=writer.name,
                )
                if nxt == writer.name:
                    reason = (
                        f"Revision requested (round {rounds}/{max_rounds}) — "
                        "sending the draft back to the Writer."
                    )
                elif verdict.verdict == "approve":
                    reason = "Draft approved — assembling the final report."
                else:
                    reason = (
                        f"Debate budget exhausted ({rounds}/{max_rounds} "
                        "rounds) — escalating to human review."
                    )
        else:
            # The decider only sees (and may only pick) this job's team —
            # minus any worker that already hit its dispatch cap. A worker
            # that ran `max_worker_dispatches` times leaves the catalog, so
            # a decider stuck on "gather more evidence" cannot loop it.
            team = state.get("team") or registry.names()
            counts: dict[str, int] = {}
            for m in state.get("transcript", []):
                if m.kind == "dispatch" and m.sender == "supervisor":
                    counts[m.recipient] = counts.get(m.recipient, 0) + 1
            cap = settings.max_worker_dispatches
            active = [w for w in team if counts.get(w, 0) < cap]
            if not active:
                nxt, reason = FINISH, "all team workers reached dispatch cap"
                target = FINISH
                msg = AgentMessage(
                    sender="supervisor", recipient=FINISH, kind="dispatch",
                    content=reason,
                )
                return {
                    "next_worker": FINISH, "pending_sends": [],
                    "transcript": [msg],
                }
            team_registry = registry.view(active)
            try:
                decision = await supervisor.decide(
                    dict(state), registry=team_registry
                )
            except Exception as exc:
                from app.core.cost_tracking import BudgetExceededError

                if not isinstance(exc, BudgetExceededError):
                    raise
                decision, nxt, reason = None, "human_review", "budget_exceeded"
                refusal = str(exc)
            if decision is not None and decision.dispatches:
                # Dynamic fan-out: the supervisor chose parallel dispatches.
                # Duplicate workers are fine in general (e.g. two researchers
                # with different mandates), but the Reviewer is the debate's
                # auditor — two parallel instances would write `latest_verdict`
                # / `debate_round` concurrently and crash the step. Keep the
                # first reviewer spec only.
                seen_reviewer = False
                pruned: list[DispatchSpec] = []
                for d in decision.dispatches:
                    if d.worker == reviewer.name:
                        if seen_reviewer:
                            continue
                        seen_reviewer = True
                    pruned.append(d)
                sends = [d.model_dump() for d in pruned]
                msgs = [
                    AgentMessage(
                        sender="supervisor", recipient=d.worker,
                        kind="dispatch",
                        content=(
                            d.mandate
                            or d.reason
                            or f"parallel {d.worker.replace('_', ' ')}"
                        ),
                        round=rounds,
                    )
                    for d in decision.dispatches
                ]
                fanout_update: dict[str, Any] = {
                    "pending_sends": sends,
                    "next_worker": None,
                    "dispatches": state.get("dispatches", 0) + len(sends),
                    "transcript": msgs,
                }
                if using_llm_decider:
                    fanout_update["prompt_versions"] = {
                        "supervisor": prompts.get("supervisor").version
                    }
                return fanout_update
            if decision is not None:
                nxt = decision.next_worker
                reason = decision.reason
                decided_via_llm = using_llm_decider

        target = FINISH if nxt == FINISH else nxt
        msg = AgentMessage(
            sender="supervisor", recipient=target, kind="dispatch",
            content=(
                reason.replace("_", " ")
                if reason
                else f"routing to {target.replace('_', ' ')}"
            ),
        )
        update: dict[str, Any] = {
            "next_worker": target,
            "pending_sends": [],
            "transcript": [msg],
        }
        if decided_via_llm:
            update["prompt_versions"] = {
                "supervisor": prompts.get("supervisor").version
            }
        if target in registry:
            update["dispatches"] = state.get("dispatches", 0) + 1
        # Evidence sealing: before the Writer (or the report assembler /
        # human review) consumes the evidence, freeze its manifest into the
        # audit chain. A changed evidence set re-seals with a new root.
        if target in ("writer", "report_assembler", "human_review"):
            from app.graph.evidence import build_evidence_manifest

            manifest = build_evidence_manifest(state.get("evidence", []))
            update["evidence_manifest"] = manifest
            if manifest["root"] != state.get("evidence_root"):
                update["evidence_root"] = manifest["root"]
                update["transcript"].append(
                    AgentMessage(
                        sender="supervisor", recipient=target,
                        kind="system",
                        # The full 64-hex root goes into the audited chain —
                        # only the human-facing report footnote truncates it.
                        content=(
                            f"Evidence sealed: {manifest['count']} sources, "
                            f"manifest root {manifest['root']}"
                        ),
                    )
                )
        if target == "human_review":
            # Persist the escalation *before* the interrupt so the checkpoint
            # already reflects awaiting_review when the graph pauses.
            from app.graph.hitl import allowed_actions

            update["status"] = "awaiting_review"
            esc_reason = "budget" if reason == "budget_exceeded" else "debate_rounds"
            update["escalation_reason"] = esc_reason
            # Consume the refusal marker: a funded resume must not see the
            # stale flag and re-escalate on its first supervisor pass.
            update["budget_blocked"] = None
            update["allowed_actions"] = allowed_actions(
                esc_reason, has_draft=bool(state.get("drafts"))
            )
            if reason == "budget_exceeded":
                content = (
                    f"LLM budget exhausted at ${state.get('cost_so_far', 0.0):.4f} "
                    f"(cap ${state.get('budget_usd') or 0.0:.4f}) — approve the "
                    "current draft, or fund more budget to continue."
                )
                if refusal:
                    content += f" Refused call: {refusal}."
            else:
                content = (
                    "Debate budget exhausted without approval"
                    + (f" — last feedback: {'; '.join(verdict.feedback)}"
                       if verdict else "")
                )
            update["transcript"].append(
                AgentMessage(
                    sender="supervisor", recipient="human",
                    kind="escalation", content=content, round=rounds,
                )
            )
        return update

    async def human_review_node(state: OrchestrationState) -> dict[str, Any]:
        """Pause for human review via LangGraph ``interrupt()``.

        First entry escalates (status awaiting_review). Resumed with
        ``Command(resume={"action": "approve"|"edit"|"reject"|"fund", ...})``:
        approve → assemble the report; edit → route back to writer with the
        human's feedback appended; reject → terminal rejection with the
        operator's reason recorded; fund/fund_and_continue → raise the
        budget and hand control back to the Supervisor.
        """
        from langgraph.types import interrupt

        from app.graph.hitl import allowed_actions

        verdict = state.get("latest_verdict")
        reason = state.get("escalation_reason") or "debate_rounds"
        has_draft = bool(state.get("drafts"))
        decision = interrupt(
            {
                "reason": reason,
                "debate_round": state.get("debate_round", 0),
                "latest_verdict": verdict.model_dump() if verdict else None,
                "has_draft": has_draft,
                # Always recompute for *this* pause — a stale set from an
                # earlier escalation would offer actions the current reason
                # doesn't support (e.g. "fund" on a debate_rounds pause).
                "allowed_actions": allowed_actions(reason, has_draft),
            }
        )
        from app.core.metrics import HUMAN_REVIEW_TOTAL

        action = str(decision.get("action", "reject")) if isinstance(decision, dict) else "reject"
        HUMAN_REVIEW_TOTAL.labels(resolution=action).inc()
        resume = AgentMessage(
            sender="human", recipient="supervisor",
            kind="system", content=f"Human decision: {action}",
            round=state.get("debate_round", 0),
        )
        if action in ("fund", "fund_and_continue"):
            # Top up the LLM budget and hand control back to the Supervisor.
            extra = float(decision.get("additional_budget_usd", 0.0))
            budget = (state.get("budget_usd") or 0.0) + max(extra, 0.0)
            return {
                "next_worker": "supervisor",
                "budget_usd": budget,
                "escalation_reason": None,
                "status": "running",
                "transcript": [AgentMessage(
                    sender="human", recipient="supervisor", kind="system",
                    content=f"Human funded +${extra:.4f} (cap now ${budget:.4f})",
                    round=state.get("debate_round", 0),
                )],
            }
        if action == "approve":
            return {
                "next_worker": "report_assembler",
                "status": "running",
                "transcript": [resume],
            }
        if action == "edit":
            feedback = str(decision.get("feedback", ""))
            if verdict is not None:
                verdict = verdict.model_copy(
                    update={"feedback": [*verdict.feedback, f"[human] {feedback}"]}
                )
            return {
                "next_worker": "writer",
                "latest_verdict": verdict,
                "status": "running",
                "transcript": [resume],
            }
        # Reject: capture the operator's reason — it lands in the
        # hash-chained transcript (audit) and the persisted job result.
        reject_reason = (
            str(decision.get("feedback", "")).strip()
            if isinstance(decision, dict)
            else ""
        ) or "no reason given"
        return {
            "next_worker": "rejection_output",
            "status": "failed",
            "rejection_reason": reject_reason,
            "transcript": [
                resume.model_copy(
                    update={
                        "content": f"Human decision: reject — {reject_reason}"
                    }
                )
            ],
            "errors": [f"rejected by human reviewer: {reject_reason}"],
        }

    async def report_assembler_node(state: OrchestrationState) -> dict[str, Any]:
        from app.core.metrics import JOBS_TOTAL
        from app.services.audit import audit_root as compute_audit_root

        report = assemble_report(dict(state))
        JOBS_TOTAL.labels(status="done" if report else "failed").inc()
        # Seal the transcript: the audit root covers every dispatch, verdict
        # and escalation up to and including this assembly event.
        seal = AgentMessage(
            sender="report_assembler", recipient="user", kind="system",
            content="Report assembled; transcript sealed.",
        )
        root = compute_audit_root([*state.get("transcript", []), seal])
        if report:
            report = report.rstrip() + f"\n\n---\n_Audit root: `{root}`_\n"
        return {
            "final_report": report,
            "audit_root": root,
            "status": "done" if report else "failed",
            "errors": [] if report else ["no draft produced"],
            "transcript": [seal],
        }

    async def output_guardrail_node(state: OrchestrationState) -> dict[str, Any]:
        """Screen the assembled report for prompt leakage before it ships."""
        from app.core.metrics import BLOCKED_REQUESTS_TOTAL
        from app.graph.guardrails import screen_output

        report = state.get("final_report")
        if report is None:
            return {}
        result = screen_output(report)
        if result.allowed:
            return {}
        BLOCKED_REQUESTS_TOTAL.inc()
        return {
            "final_report": (
                "The generated report was withheld by the output guardrail "
                f"({result.reason})."
            ),
            "errors": [f"output_guardrail: {result.reason}"],
        }

    def route_after_guardrail(state: OrchestrationState) -> str:
        return "rejection_output" if state.get("status") == "blocked" else "supervisor"

    async def rejection_output_node(state: OrchestrationState) -> dict[str, Any]:
        return {"final_report": None}

    builder = StateGraph(OrchestrationState)
    builder.add_node("input_guardrail", _timed("input_guardrail", input_guardrail_node))
    builder.add_node("supervisor", _timed("supervisor", supervisor_node))
    builder.add_node("human_review", _timed("human_review", human_review_node))
    builder.add_node("report_assembler", _timed("report_assembler", report_assembler_node))
    builder.add_node("output_guardrail", _timed("output_guardrail", output_guardrail_node))
    builder.add_node("rejection_output", _timed("rejection_output", rejection_output_node))
    for worker in registry:
        builder.add_node(worker.name, _as_node(worker))

    builder.add_edge(START, "input_guardrail")
    builder.add_conditional_edges(
        "input_guardrail",
        route_after_guardrail,
        {"supervisor": "supervisor", "rejection_output": "rejection_output"},
    )
    def route_from_supervisor(state: OrchestrationState) -> Any:
        """Fan-out wins: pending dispatches become parallel ``Send``s whose
        per-instance state carries the supervisor-assigned mandate."""
        from langgraph.types import Send

        sends = state.get("pending_sends") or []
        if sends:
            return [
                Send(
                    d["worker"],
                    {
                        **dict(state),
                        "mandate": d.get("mandate", ""),
                        "pending_sends": [],
                    },
                )
                for d in sends
            ]
        return state.get("next_worker") or END

    builder.add_conditional_edges(
        "supervisor", route_from_supervisor, _supervisor_targets(registry)
    )
    for worker in registry:
        builder.add_edge(worker.name, "supervisor")
    builder.add_conditional_edges(
        "human_review",
        lambda s: s.get("next_worker") or "rejection_output",
        {
            "report_assembler": "report_assembler",
            "writer": "writer",
            "supervisor": "supervisor",
            "rejection_output": "rejection_output",
        },
    )
    builder.add_edge("report_assembler", "output_guardrail")
    builder.add_edge("output_guardrail", END)
    builder.add_edge("rejection_output", END)

    return builder.compile(checkpointer=checkpointer)


def initial_state(
    *,
    job_id: str,
    workspace_id: str,
    brief: Any,
    max_debate_rounds: int,
    budget_usd: float | None = None,
) -> dict[str, Any]:
    """Seed state for a new orchestration job."""
    return {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "brief": brief,
        "status": "queued",
        "sub_questions": [],
        "evidence": [],
        "drafts": [],
        "transcript": [],
        "analyst_notes": [],
        "debate_round": 0,
        "max_debate_rounds": max_debate_rounds,
        "latest_verdict": None,
        "next_worker": None,
        "final_report": None,
        "errors": [],
        "team": team_for(brief.report_type),
        "dispatches": 0,
        "cost_so_far": 0.0,
        "budget_usd": (
            budget_usd if budget_usd is not None else brief.budget_usd
        ),
        "escalation_reason": None,
        "audit_root": None,
        "prompt_versions": {},
    }
