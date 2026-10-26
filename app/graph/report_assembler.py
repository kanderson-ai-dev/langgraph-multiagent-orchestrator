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
    kept, _dropped, hashes, _unknown = verify_citations(draft, evidence)

    # The draft's own `## Sources` (required by the section template but
    # author-invented) is replaced by the canonical verified list below —
    # otherwise the report would carry two Sources sections.
    md_lines = draft.markdown.strip().splitlines()
    for i, ln in enumerate(md_lines):
        if ln.lstrip().startswith("#") and ln.lstrip("#").strip().lower() == "sources":
            md_lines = md_lines[:i]
            break
    parts = [f"# {draft.title}", "", "\n".join(md_lines).strip()]
    if kept:
        parts += ["", "## Sources", ""]
        for c, h in zip(kept, hashes, strict=True):
            short = f" `#{h[:12]}`" if h else ""
            ref = f"[{c.ref}] " if c.ref else ""
            parts.append(f"- {ref}{c.claim} — {c.source_url}{short}")
    return "\n".join(parts).strip() + "\n"
