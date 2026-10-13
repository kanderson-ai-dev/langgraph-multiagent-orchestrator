"""HITL semantics — which review actions are legal per escalation reason.

``allowed_actions`` is the single source of truth shared by the graph (it
ships inside the ``interrupt()`` payload), the runner (it persists it on
the paused job's result), and the API route (it validates the human's
decision against it). Keeping it pure keeps all three layers aligned.
"""

from __future__ import annotations

# Actions every review UI may offer. `fund_and_continue` is an explicit
# alias of `fund`: top the job's LLM budget up and hand control back to
# the Supervisor.
ALL_ACTIONS = ("approve", "edit", "reject", "fund", "fund_and_continue")


def allowed_actions(escalation_reason: str | None, has_draft: bool) -> list[str]:
    """Return the review actions that make sense for this pause.

    - ``budget`` without a draft: there is nothing to approve — only
      ``fund``/``fund_and_continue`` (resume paid work) or ``reject``.
    - ``budget`` with a draft: the human may also approve/edit what exists.
    - ``debate_rounds`` (or anything else): approve/edit require a draft;
      without one only edit (which sends guidance back to the writer) or
      reject are meaningful.
    """
    if escalation_reason == "budget":
        actions: list[str] = []
        if has_draft:
            actions += ["approve", "edit"]
        actions += ["fund", "fund_and_continue", "reject"]
        return actions
    if has_draft:
        return ["approve", "edit", "reject"]
    return ["edit", "reject"]


def approve_hint(escalation_reason: str | None) -> str:
    """Guided alternative when ``approve`` is requested without a draft."""
    if escalation_reason == "budget":
        return (
            "no draft exists to approve — fund the job to continue paid "
            "work (action 'fund' or 'fund_and_continue' with "
            "additional_budget_usd > 0), or 'reject' it"
        )
    return (
        "no draft exists to approve — use 'edit' to send guidance back to "
        "the Writer, or 'reject' to end the job"
    )
