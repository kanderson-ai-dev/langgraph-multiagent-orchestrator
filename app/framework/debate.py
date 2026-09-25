"""Bounded debate loop — guaranteed termination by construction.

Two roles exchange rounds (e.g. Writer proposes, Reviewer returns a verdict).
The loop is bounded by ``max_rounds``: when the budget is exhausted the
outcome is ``ESCALATE``, never another round — termination is a structural
property, tested, not assumed.
"""

from enum import StrEnum
from typing import Literal


class DebateOutcome(StrEnum):
    """Terminal states of the debate loop."""

    APPROVED = "approved"  # reviewer approved within budget
    ESCALATE = "escalate"  # budget exhausted → human review
    ABORTED = "aborted"  # unrecoverable error


class DebateController:
    """Tracks rounds and decides whether the debate may continue.

    ``round`` counts completed propose→verdict cycles. ``decide`` returns the
    next step as a routing hint for the Supervisor: the name of the proposing
    worker (another round), ``FINISH`` (approved), or the escalation target.
    """

    def __init__(
        self, *, proposer: str, max_rounds: int, escalate_to: str = "human_review"
    ) -> None:
        if max_rounds < 0:
            msg = "max_rounds must be >= 0"
            raise ValueError(msg)
        self.proposer = proposer
        self.max_rounds = max_rounds
        self.escalate_to = escalate_to
        self.round = 0
        self.outcome: DebateOutcome | None = None

    @property
    def rounds_left(self) -> int:
        return max(0, self.max_rounds - self.round)

    @property
    def exhausted(self) -> bool:
        return self.round >= self.max_rounds

    def record_round(self) -> int:
        """Increment and return the new round count."""
        self.round += 1
        return self.round

    def decide(self, approved: bool) -> str:
        """Resolve the next hop after a verdict.

        Returns the proposer's name (revise → another round), ``FINISH``
        (approved), or the escalation target (budget exhausted). Once a
        terminal outcome is recorded, subsequent calls keep returning it —
        the loop cannot re-open.
        """
        from app.framework.registry import FINISH

        if self.outcome is not None:
            return self._terminal_target()
        if approved:
            self.outcome = DebateOutcome.APPROVED
            return FINISH
        if self.exhausted:
            self.outcome = DebateOutcome.ESCALATE
            return self.escalate_to
        return self.proposer

    def abort(self) -> str:
        self.outcome = DebateOutcome.ABORTED
        return self._terminal_target()

    def _terminal_target(self) -> str:
        from app.framework.registry import FINISH

        return FINISH if self.outcome is DebateOutcome.APPROVED else self.escalate_to


def decide_next_after_verdict(
    *, approved: bool, rounds_used: int, max_rounds: int,
    proposer: str, escalate_to: str = "human_review",
) -> Literal["FINISH", "human_review"] | str:
    """Functional variant used by graph edges: pure next-hop decision."""
    from app.framework.registry import FINISH

    if approved:
        return FINISH
    if rounds_used >= max_rounds:
        return escalate_to
    return proposer
