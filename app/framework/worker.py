"""Worker protocol — the contract every specialized agent implements.

Domain-agnostic: the framework knows nothing about researchers, writers or
reviewers. A worker is a named unit of work that receives the shared state
and returns a partial state update (the LangGraph node-result convention).
"""

from typing import Any, Protocol, runtime_checkable

# A partial state update, as LangGraph nodes return.
StateUpdate = dict[str, Any]


@runtime_checkable
class Worker(Protocol):
    """A specialized agent the Supervisor can dispatch work to."""

    @property
    def name(self) -> str:
        """Unique identifier used in routing decisions."""
        ...

    @property
    def description(self) -> str:
        """One-line capability summary shown to the Supervisor LLM."""
        ...

    async def run(self, state: dict[str, Any]) -> StateUpdate:
        """Execute the worker's task and return a partial state update."""
        ...
