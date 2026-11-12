"""Drift guard — keeps the README architecture diagrams honest.

The runtime-flow block is generated from the compiled graph
(``scripts/gen_architecture.py --write``) and the system view is curated by
hand; both must agree with the code they describe. Fully offline and
deterministic — the graph is built on ``StubLLM`` with fixture tools.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.graph.graph import build_graph
from app.graph.teams import CORE_TEAM, SPECIALIST_ROLES
from app.services.llm_client import StubLLM
from app.services.prompt_registry import DEFAULT_PROMPTS_DIR
from scripts import gen_architecture as gen

README = Path(__file__).resolve().parent.parent / "README.md"

# Diagram-only node ids in the runtime block and why they exist —
# everything else must be a real compiled-graph node name. ``workers`` is
# the Send() fan-out: real routing behaviour with no dedicated node.
RUNTIME_ALIASES = {"workers"}
ALIAS_EDGES = {("supervisor", "workers"), ("workers", "supervisor")}

# Prompt consumers: every worker the Supervisor may route to, plus the
# Supervisor decider itself (deciders.py renders the "supervisor" prompt).
ROLES = set(CORE_TEAM) | set(SPECIALIST_ROLES) | {"supervisor"}

# Seed names allowed in prompts/ beyond the consumers — `judge` is the
# eval-only prompt (app/services/prompt_eval.py), never rendered by an agent.
EVAL_ONLY_PROMPTS = {"judge"}

_ARROW = (
    r"(<-->|--\s*\"[^\"]*\"\s*-->|-\.\s*\"[^\"]*\"\s*\.->|-.->|-->)"
)
_EDGE_HEAD = re.compile(
    rf"^(\w+(?:\s*&\s*\w+)*)\s*{_ARROW}\s*(.*)$"
)
_ID_LIST = re.compile(r"^\s*(\w+(?:\s*&\s*\w+)*)")
_NODE_DECL = re.compile(r"^(\w+)\s*[\(\[\"]")


class _FakeSearch:
    async def search(self, query: str, *, max_results: int, workspace_id: str = "") -> list:
        return []


class _FakeScraper:
    async def scrape(self, url: str, *, workspace_id: str = "") -> Any:
        raise RuntimeError("offline")


def _compiled_nodes_edges() -> tuple[set[str], set[tuple[str, str]]]:
    graph = build_graph(
        Settings(_env_file=None),
        llm=StubLLM(),
        search=_FakeSearch(),  # type: ignore[arg-type]
        scraper=_FakeScraper(),  # type: ignore[arg-type]
    ).get_graph()
    return set(graph.nodes), {(e.source, e.target) for e in graph.edges}


def _runtime_block() -> str:
    text = README.read_text(encoding="utf-8")
    assert text.count(gen.BEGIN) == 1 and text.count(gen.END) == 1, (
        "README.md must contain exactly one GENERATED: runtime-flow marker pair"
    )
    inner = text.split(gen.BEGIN, 1)[1].split(gen.END, 1)[0]
    return inner.replace("```mermaid", "").replace("```", "").strip()


def _architecture_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## 🏗️ Architecture")
    return text[start : text.index("\n## ", start + 1)]


def _system_block() -> str:
    """The mermaid block in the Architecture section that isn't generated."""
    blocks = re.findall(r"```mermaid\n(.*?)```", _architecture_section(), re.DOTALL)
    candidates = [b for b in blocks if b.strip() != _runtime_block()]
    assert len(candidates) == 1, "expected exactly one curated system-view diagram"
    return candidates[0]


def _parse_mermaid(block: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Extract node ids and directed edges from a flowchart block.

    Handles node decls (``id(...)`` / ``id["..."]`` / ``id[(...)]``),
    ``A --> B``, dashed ``-.->`` / labelled ``-. "x" .->``, bidirectional
    ``<-->``, ``&`` source/target lists, and chained ``A --> B --> C``.
    """
    nodes: set[str] = set()
    edges: set[tuple[str, str]] = set()
    in_frontmatter = False
    queue = [line.strip() for line in block.splitlines()]
    while queue:
        s = queue.pop(0)
        if not s:
            continue
        if s == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or s.startswith(("%%", "classDef", "class ")):
            continue
        m = _EDGE_HEAD.match(s)
        if not m:
            decl = _NODE_DECL.match(s)
            if decl:
                nodes.add(decl.group(1))
            continue
        srcs = [p.strip() for p in m.group(1).split("&")]
        arrow, rest = m.group(2), m.group(3)
        dm = _ID_LIST.match(rest)
        if not dm:
            continue
        dsts = [p.strip() for p in dm.group(1).split("&")]
        for a in srcs:
            for b in dsts:
                edges.add((a, b))
                if arrow == "<-->":
                    edges.add((b, a))
        nodes.update(srcs)
        nodes.update(dsts)
        tail = rest[dm.end() :]
        if tail.strip():
            # Remainder after the target list — a shape (``["x"]:::cls``) or
            # a chained edge (``--> C``). Re-queue from the last target.
            queue.append(f"{dsts[-1]}{tail}")
    return nodes, edges


def test_runtime_block_matches_generator() -> None:
    """The committed block is byte-identical to a fresh generation."""
    expected = gen.generate_runtime_block().strip()
    assert _runtime_block() == expected, (
        "README runtime diagram is stale — run "
        "`uv run python scripts/gen_architecture.py --write`"
    )


def test_runtime_nodes_match_compiled_graph() -> None:
    """Every compiled node appears; every diagram node is real or aliased."""
    compiled, _ = _compiled_nodes_edges()
    nodes, _ = _parse_mermaid(_runtime_block())
    missing = compiled - nodes
    unknown = nodes - compiled - RUNTIME_ALIASES
    assert not missing, f"graph nodes missing from the diagram: {sorted(missing)}"
    assert not unknown, (
        f"diagram nodes that are neither real nor documented aliases: "
        f"{sorted(unknown)} (whitelist lives in RUNTIME_ALIASES)"
    )


def test_runtime_edges_match_compiled_graph() -> None:
    """Both directions pinned: topology edits must regenerate the block."""
    _, compiled_edges = _compiled_nodes_edges()
    _, edges = _parse_mermaid(_runtime_block())
    missing = compiled_edges - edges
    extra = edges - compiled_edges - ALIAS_EDGES
    assert not missing, f"compiled edges missing from the diagram: {sorted(missing)}"
    assert not extra, f"diagram edges not in the compiled graph: {sorted(extra)}"


def test_system_view_has_registry_and_role_edges() -> None:
    """Every prompt consumer has a visible dependency on the registry."""
    nodes, edges = _parse_mermaid(_system_block())
    assert "registry" in nodes, "system view must name the Prompt Registry node"
    for role in sorted(ROLES):
        assert role in nodes, f"system view missing consumer node {role!r}"
        assert (role, "registry") in edges, (
            f"system view missing {role} -> registry dependency edge"
        )


def test_prompt_seed_names_match_diagram_roles() -> None:
    """prompts/<name>/ seeds are exactly the consumers shown, plus judge."""
    seeds = {p.parent.name for p in DEFAULT_PROMPTS_DIR.glob("*/v*.yaml")}
    assert seeds - EVAL_ONLY_PROMPTS == ROLES, (
        f"seed names {sorted(seeds)} != diagram roles {sorted(ROLES)} "
        f"+ eval-only {sorted(EVAL_ONLY_PROMPTS)}"
    )
