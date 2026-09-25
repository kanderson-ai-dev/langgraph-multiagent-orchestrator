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

from collections.abc import Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.config import Settings
from app.framework import (
    FINISH,
    RouterDecider,
    Supervisor,
    Worker,
    WorkerRegistry,
    decide_next_after_verdict,
)
from app.graph.deciders import make_llm_decider, rule_based_decider
from app.graph.nodes.researcher import ResearchPlan
from app.graph.nodes.reviewer import ReviewOutput
from app.graph.nodes.specialist import SpecialistAnalysis
from app.graph.nodes.writer import DraftOutput
from app.graph.report_assembler import assemble_report
from app.graph.state import AgentMessage, OrchestrationState
from app.graph.teams import build_all_workers, team_for
from app.services.llm_client import StructuredLLM, StubLLM
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
        return await worker.run(dict(state))

    return node


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
            verdict="approve", score=4.0, rubric={"structure": 4.0}
        ),
    )
    llm.register_default(
        "SpecialistAnalysis",
        lambda s, c: SpecialistAnalysis(
            findings=["offline stub finding"],
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
) -> CompiledStateGraph[Any]:
    """Compile the orchestration graph. Injectable deps keep it testable."""

    registry = WorkerRegistry()
    # Every known worker is a node; each job's `team` (from report_type)
    # narrows what the Supervisor may route to.
    for worker in build_all_workers(
        llm=llm, search=search, scraper=scraper, settings=settings
    ):
        registry.register(worker)
    writer = registry.get("writer")
    reviewer = registry.get("reviewer")

    if isinstance(llm, StubLLM):
        _register_stub_builders(llm)

    if decider is None:
        decider = (
            rule_based_decider
            if isinstance(llm, StubLLM)
            else make_llm_decider(llm)
        )
    supervisor = Supervisor(registry, decider, max_dispatches=25)

    async def input_guardrail_node(state: OrchestrationState) -> dict[str, Any]:
        """Screen the brief before any paid call — blocked input burns 0 tokens."""
        from app.graph.guardrails import screen_brief

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

        # Cost governance: a spent budget stops all further paid work before
        # the decider itself burns tokens. An approved draft may still finish.
        budget = state.get("budget_usd")
        over_budget = budget is not None and state.get("cost_so_far", 0.0) >= budget
        if over_budget and not (verdict and verdict.verdict == "approve"):
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
                nxt = decide_next_after_verdict(
                    approved=verdict.verdict == "approve",
                    rounds_used=rounds,
                    max_rounds=max_rounds,
                    proposer=writer.name,
                )
                reason = f"verdict={verdict.verdict} round={rounds}/{max_rounds}"
        else:
            # The decider only sees (and may only pick) this job's team.
            team_registry = registry.view(state.get("team") or registry.names())
            decision = await supervisor.decide(dict(state), registry=team_registry)
            if decision.dispatches:
                # Dynamic fan-out: the supervisor chose parallel dispatches.
                sends = [d.model_dump() for d in decision.dispatches]
                msgs = [
                    AgentMessage(
                        sender="supervisor", recipient=d.worker,
                        kind="dispatch",
                        content=d.mandate or d.reason or f"parallel {d.worker}",
                        round=rounds,
                    )
                    for d in decision.dispatches
                ]
                return {
                    "pending_sends": sends,
                    "next_worker": None,
                    "dispatches": state.get("dispatches", 0) + len(sends),
                    "transcript": msgs,
                }
            nxt = decision.next_worker
            reason = decision.reason

        target = FINISH if nxt == FINISH else nxt
        msg = AgentMessage(
            sender="supervisor", recipient=target, kind="dispatch",
            content=reason or f"routing to {target}",
        )
        update: dict[str, Any] = {
            "next_worker": target,
            "pending_sends": [],
            "transcript": [msg],
        }
        if target in registry:
            update["dispatches"] = state.get("dispatches", 0) + 1
        if target == "human_review":
            # Persist the escalation *before* the interrupt so the checkpoint
            # already reflects awaiting_review when the graph pauses.
            update["status"] = "awaiting_review"
            update["escalation_reason"] = (
                "budget" if reason == "budget_exceeded" else "debate_rounds"
            )
            if reason == "budget_exceeded":
                content = (
                    f"LLM budget exhausted at ${state.get('cost_so_far', 0.0):.4f} "
                    f"(cap ${state.get('budget_usd') or 0.0:.4f}) — approve the "
                    "current draft, or fund more budget to continue."
                )
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
        ``Command(resume={"action": "approve"|"edit"|"reject", ...})``:
        approve → assemble the report; edit → route back to writer with the
        human's feedback appended; reject → terminal rejection.
        """
        from langgraph.types import interrupt

        verdict = state.get("latest_verdict")
        decision = interrupt(
            {
                "reason": "debate_rounds_exhausted",
                "debate_round": state.get("debate_round", 0),
                "latest_verdict": verdict.model_dump() if verdict else None,
                "actions": ["approve", "edit", "reject", "fund"],
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
        if action == "fund":
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
        return {
            "next_worker": "rejection_output",
            "status": "failed",
            "transcript": [resume],
            "errors": ["rejected by human reviewer"],
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
    builder.add_node("input_guardrail", input_guardrail_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("report_assembler", report_assembler_node)
    builder.add_node("output_guardrail", output_guardrail_node)
    builder.add_node("rejection_output", rejection_output_node)
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
    }
