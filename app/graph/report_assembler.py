"""Report assembler — turns the approved draft into the final artifact.

Re-verifies every citation against fetched evidence and strips unsupported
ones (defense in depth: the Reviewer already counted them, the assembler
guarantees nothing unverified ships), then appends a sources section.
"""

from typing import Any

from app.graph.nodes.reviewer import verify_citations
from app.graph.state import ReportDraft


def assemble_report(state: dict[str, Any]) -> str | None:
    """Build the final Markdown report from the latest draft, or None."""
    drafts: list[ReportDraft] = state.get("drafts", [])
    if not drafts:
        return None
    draft = drafts[-1]
    evidence = state.get("evidence", [])
    kept, _dropped = verify_citations(draft, evidence)

    parts = [f"# {draft.title}", "", draft.markdown.strip()]
    if kept:
        parts += ["", "## Sources", ""]
        for c in kept:
            parts.append(f"- {c.claim} — {c.source_url}")
    return "\n".join(parts).strip() + "\n"
