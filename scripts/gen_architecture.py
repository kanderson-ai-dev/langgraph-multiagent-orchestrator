"""Regenerate the README runtime-flow Mermaid block from the compiled graph.

The runtime diagram under ``README.md`` "Architecture" is generated, not
hand-drawn: this script builds the orchestration graph fully offline
(``StubLLM`` + fixture tools — no credentials, no network) and renders the
compiled topology via ``get_graph().draw_mermaid()``. A thin curation layer
then:

- relabels the ``__start__`` / ``__end__`` sentinels as START / END and gives
  a few nodes human-facing labels (node ids stay the real graph names),
- labels the conditional (dashed) edges with their routing semantics,
- adds the ``workers`` alias node for the dynamic ``Send()`` fan-out — a
  runtime mechanism with no graph node of its own (documented alias, the
  only one the drift test allows).

Prints the block to stdout; ``--write`` replaces the fenced block between
``<!-- BEGIN GENERATED: runtime-flow -->`` and
``<!-- END GENERATED: runtime-flow -->`` markers in ``README.md``.
Nothing is written unless ``--write`` is passed.

Usage::

    uv run python scripts/gen_architecture.py
    uv run python scripts/gen_architecture.py --write
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.graph.graph import build_graph  # noqa: E402
from app.services.llm_client import StubLLM  # noqa: E402

README = ROOT / "README.md"
BEGIN = "<!-- BEGIN GENERATED: runtime-flow -->"
END = "<!-- END GENERATED: runtime-flow -->"

# Human-facing display labels — node ids stay the real graph names so the
# drift test can match them verbatim against ``get_graph().nodes``.
NODE_LABELS = {
    "__start__": "START",
    "__end__": "END",
    "supervisor": "Supervisor<br/>LLM router",
    "human_review": "human_review<br/>interrupt()",
}

# Semantics of the conditional (dashed) edges, keyed by (source, target).
# Conditional edges absent here stay unlabeled; solid edges (unconditional
# flow) are never labeled — dashes are what carries the routing story.
EDGE_LABELS = {
    ("input_guardrail", "rejection_output"): "blocked",
    ("input_guardrail", "supervisor"): "safe",
    ("supervisor", "human_review"): "budget / rounds exhausted",
    ("supervisor", "report_assembler"): "approved",
    ("supervisor", "researcher"): "next_worker",
    ("supervisor", "writer"): "next_worker",
    ("supervisor", "reviewer"): "next_worker",
    ("supervisor", "financial_analyst"): "next_worker",
    ("supervisor", "compliance_analyst"): "next_worker",
    ("human_review", "supervisor"): "fund",
    ("human_review", "writer"): "edit",
    ("human_review", "report_assembler"): "approve",
    ("human_review", "rejection_output"): "reject",
}

# ``Send()`` fan-out is real routing behaviour with no dedicated node —
# surfaced as the single documented alias so the diagram stays honest AND
# readable. ``tests/test_architecture_diagram.py`` whitelists this name.
FANOUT = (
    '\tsupervisor -. "Send() fan-out" .-> '
    'workers["workers ×N<br/>parallel mandates"];',
    "\tworkers --> supervisor;",
)

_NODE_RE = re.compile(r"^(\w+)(\(?\[?.*\)?\]?)(:::\w+)?$")
_EDGE_RE = re.compile(r"^(\w+)\s+(-->|-\.->|-\..*?\.->)\s+(\w+);$")


class _FakeSearch:
    async def search(self, query: str, *, max_results: int, workspace_id: str = "") -> list:
        return []


class _FakeScraper:
    async def scrape(self, url: str, *, workspace_id: str = "") -> None:
        raise RuntimeError("offline — diagram generation never scrapes")


def _node_line(raw: str) -> str:
    """Apply display-label overrides to one ``id(label):::class`` line."""
    m = _NODE_RE.match(raw)
    if not m:
        return f"\t{raw}"
    node_id, label = m.group(1), NODE_LABELS.get(m.group(1))
    if label is None:
        return f"\t{raw}"
    if node_id in ("__start__", "__end__"):
        return f"\t{node_id}([{label}])"
    return f'\t{node_id}("{label}")'


def _edge_line(raw: str) -> str:
    """Label a conditional edge with its routing semantics."""
    m = _EDGE_RE.match(raw)
    if not m:
        return raw
    src, arrow, dst = m.group(1), m.group(2), m.group(3)
    if arrow == "-->":
        return f"\t{raw}"
    label = EDGE_LABELS.get((src, dst))
    if label is None:
        # Keep any compiled label (minus &nbsp; padding) — e.g. a future
        # mapping value like FINISH that has no curated wording yet.
        inner = re.sub(r"&nbsp;|\s+", " ", arrow).replace("-.", "").replace(".->", "").strip()
        return f'\t{src} -. "{inner}" .-> {dst};' if inner else f"\t{src} -.-> {dst};"
    return f'\t{src} -. "{label}" .-> {dst};'


def generate_runtime_block() -> str:
    """Build the graph offline and return the curated Mermaid block body."""
    settings = Settings(_env_file=None)
    graph = build_graph(
        settings,
        llm=StubLLM(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
    ).get_graph()

    out: list[str] = []
    frontmatter_delims = 0
    fanout_done = False
    for line in graph.draw_mermaid().splitlines():
        stripped = line.strip()
        if stripped == "---" and frontmatter_delims < 2:
            frontmatter_delims += 1
            out.append(line)
            continue
        if frontmatter_delims == 1 or not stripped:
            out.append(line)
            continue
        if stripped.startswith(("graph ", "flowchart ")):
            out.append(line)
            continue
        if _EDGE_RE.match(stripped):
            out.append(_edge_line(stripped))
            continue
        if stripped.startswith("classDef"):
            # Drop theme fills entirely — the renderer's default theme
            # (dark on GitHub dark mode) reads better than LangGraph's
            # pastel classDefs, matching the pre-generated diagram.
            if not fanout_done:
                out.extend(FANOUT)
                fanout_done = True
            continue
        out.append(_node_line(stripped))
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace the marked block in README.md (default: print to stdout)",
    )
    args = parser.parse_args()

    block = generate_runtime_block()
    if not args.write:
        print(block, end="")
        return 0

    readme = README.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    replacement = f"{BEGIN}\n```mermaid\n{block}```\n{END}"
    new, count = pattern.subn(replacement, readme)
    if count != 1:
        print(
            f"error: expected exactly one {BEGIN} ... {END} block in README.md "
            f"(found {count})",
            file=sys.stderr,
        )
        return 1
    README.write_text(new, encoding="utf-8")
    print(f"updated runtime-flow diagram in {README.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
