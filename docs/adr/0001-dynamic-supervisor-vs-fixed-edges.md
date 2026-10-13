# ADR 0001 — Dynamic Supervisor vs fixed graph edges

**Status:** accepted · **Date:** 2026-10

## Context

The portfolio ladder already contains a fixed
planner→workers→critic→writer pipeline (`agentic-web-researcher`). A
fourth project repeating that topology adds no signal. The question: does
routing between agents deserve to be an LLM decision at runtime, or should
the graph topology itself encode the flow?

## Decision

The Supervisor is an **LLM-based dynamic router**, not a set of static
edges. It sees the live `OrchestrationState` (drafts, evidence, verdicts,
budget, dispatch counts) and emits a structured decision — `next_worker`
or parallel `Send()` fan-out — each turn. `app/framework/supervisor.py`
holds the domain-agnostic decider protocol; `app/graph/` plugs the report
team onto it.

Pathological loops are guarded **structurally**, not by hoping the decider
behaves: per-worker dispatch caps (`MAX_WORKER_DISPATCHES`), global round
bounds (`MAX_DEBATE_ROUNDS`), and pre-dispatch budget enforcement. When
bounds exhaust, control passes to a real LangGraph `interrupt()` — a human
decides.

## Consequences

- **+** Teams assemble per `report_type` at runtime; adding a specialist
  is a class + a registry line, no graph surgery.
- **+** The Supervisor can skip research entirely on a trivial brief, or
  fan out 3 researchers on a wide one — the topology is a decision, not a
  diagram.
- **−** Non-determinism: routing quality depends on the model. Mitigated
  by EDD gates (convergence, blocked-rate) and deterministic stubs in CI.
- **−** Every dispatch costs tokens; bounded by `budget_usd` enforcement
  before each call.
