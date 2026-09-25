"""Reusable, domain-agnostic Supervisor-Workers orchestration primitives."""

from app.framework.debate import (
    DebateController,
    DebateOutcome,
    decide_next_after_verdict,
)
from app.framework.registry import FINISH, WorkerRegistry
from app.framework.supervisor import RouterDecider, RoutingDecision, Supervisor
from app.framework.worker import StateUpdate, Worker

__all__ = [
    "FINISH",
    "DebateController",
    "DebateOutcome",
    "RouterDecider",
    "RoutingDecision",
    "StateUpdate",
    "Supervisor",
    "Worker",
    "WorkerRegistry",
    "decide_next_after_verdict",
]
