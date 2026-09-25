"""Team composition — which workers a report type deploys.

This is the "dynamic team" layer: the graph compiles every known worker as a
node, but each job's ``team`` (derived from ``brief.report_type``) limits what
the Supervisor may route to. Specialists are plug-and-play ``Worker``
implementations — writing one is a class plus a registration line, which is
what makes ``app/framework/`` a real framework rather than a fixed graph.
"""

from typing import Any

from app.core.config import Settings
from app.framework.worker import Worker

# Canonical ordering inside a team: researcher → specialists → writer →
# reviewer. The deterministic offline decider follows this order.
CORE_TEAM = ["researcher", "writer", "reviewer"]

# report_type → specialist worker names inserted between researcher and writer.
TEAMS: dict[str, list[str]] = {
    "general": [],
    "market_intelligence": ["financial_analyst"],
    "vendor_assessment": ["financial_analyst", "compliance_analyst"],
    "competitive_landscape": ["financial_analyst"],
    "technical_due_diligence": ["compliance_analyst"],
}

REPORT_TYPES: tuple[str, ...] = tuple(TEAMS)


def team_for(report_type: str) -> list[str]:
    """Ordered worker names for a report type (``general`` on unknown)."""
    specialists = TEAMS.get(report_type, [])
    return ["researcher", *specialists, "writer", "reviewer"]


def specialists_of(report_type: str) -> list[str]:
    return list(TEAMS.get(report_type, []))


# Specialist definitions: (description shown to the Supervisor, system focus).
SPECIALIST_ROLES: dict[str, tuple[str, str]] = {
    "financial_analyst": (
        "Analyzes evidence for financial signals: pricing, unit economics, "
        "funding, TAM, revenue models and budget risk",
        "You are the Financial Analyst on a report team. Read the gathered "
        "evidence and extract the financially material facts: pricing models, "
        "cost drivers, market size signals, funding/revenue indicators, and "
        "budget or ROI risks. Be specific — cite numbers when present.",
    ),
    "compliance_analyst": (
        "Analyzes evidence for regulatory/compliance exposure: obligations, "
        "deadlines, certifications, legal risk",
        "You are the Compliance Analyst on a report team. Read the gathered "
        "evidence and extract regulatory obligations, deadlines, certification "
        "requirements, jurisdictional differences, and legal exposure. Flag "
        "anything that could create liability.",
    ),
}

# Section templates the Writer follows per report type.
SECTION_TEMPLATES: dict[str, str] = {
    "general": "Executive Summary · Findings · Analysis · Recommendation · Sources",
    "market_intelligence": (
        "Executive Summary · Market Overview · Demand Signals · Competitive "
        "Dynamics · Financial Signals · Outlook · Sources"
    ),
    "vendor_assessment": (
        "Executive Summary · Vendor Profile · Capability Assessment · "
        "Financial Health · Compliance & Risk Posture · Recommendation · Sources"
    ),
    "competitive_landscape": (
        "Executive Summary · Landscape Overview · Competitor Profiles · "
        "Differentiation & Positioning · Pricing & Economics · "
        "Strategic Implications · Sources"
    ),
    "technical_due_diligence": (
        "Executive Summary · Technology Assessment · Architecture & "
        "Scalability · Security & Compliance Posture · Risks & Gaps · "
        "Recommendation · Sources"
    ),
}


def build_all_workers(
    *,
    llm: Any,
    search: Any,
    scraper: Any,
    settings: Settings,
) -> list[Worker]:
    """Instantiate every worker the superset graph needs.

    The graph registers *all* workers as nodes; ``team`` state (from
    ``team_for``) restricts routing per job. Specialists are built generically
    from ``SPECIALIST_ROLES``.
    """
    from app.graph.nodes.researcher import ResearcherWorker
    from app.graph.nodes.reviewer import ReviewerWorker
    from app.graph.nodes.specialist import SpecialistWorker
    from app.graph.nodes.writer import WriterWorker

    workers: list[Worker] = [
        ResearcherWorker(llm, search, scraper, max_queries=settings.max_sub_questions),
        WriterWorker(llm),
        ReviewerWorker(llm),
    ]
    for name, (description, focus) in SPECIALIST_ROLES.items():
        workers.append(SpecialistWorker(name, description, focus, llm))
    return workers
