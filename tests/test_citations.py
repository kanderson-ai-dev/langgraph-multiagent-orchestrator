"""Citation verification: verbatim, punctuation-insensitive, fuzzy window."""

from app.graph.citations import citation_supported, support_rate

SOURCE = (
    "LangGraph is a framework for building stateful, multi-agent applications "
    "with LLMs. It supports checkpointing and human-in-the-loop interrupts."
)


def test_verbatim_match() -> None:
    assert citation_supported("multi-agent applications", SOURCE)


def test_punctuation_insensitive() -> None:
    assert citation_supported("stateful multi agent applications", SOURCE)
    assert citation_supported("checkpointing, and human-in-the-loop, interrupts", SOURCE)


def test_fuzzy_window_near_match() -> None:
    # Mostly-correct quote with a couple of word drifts should pass ≥0.85.
    quote = "LangGraph is a framework for building stateful multi-agent applications with models"
    assert citation_supported(quote, SOURCE)


def test_fabricated_quote_rejected() -> None:
    assert not citation_supported(
        "LangGraph was invented by the ancient Romans", SOURCE
    )


def test_empty_inputs_rejected() -> None:
    assert not citation_supported("", SOURCE)
    assert not citation_supported("quote", "")
    assert not citation_supported("", "")


def test_support_rate() -> None:
    assert support_rate(9, 10) == 0.9
    assert support_rate(0, 0) == 0.0
    assert support_rate(3, 0) == 0.0


# ─── source-binding: a quote must verify against the CITED source ────────────


def test_citation_must_match_cited_source_only() -> None:
    """A quote present in a *different* source does not support the claim —
    no pooling across the evidence bundle."""
    from app.graph.nodes.reviewer import verify_citations
    from app.graph.state import Citation, EvidenceItem, ReportDraft

    ev = [
        EvidenceItem(
            evidence_id="e1", url="https://a.test",
            text="alpha quote text", sha256="a" * 64,
        ),
        EvidenceItem(
            evidence_id="e2", url="https://b.test",
            text="beta quote text", sha256="b" * 64,
        ),
    ]
    draft = ReportDraft(
        version=1, title="t", markdown="m",
        citations=[
            # quote lives in b.test but the citation credits a phantom URL
            Citation(
                claim="x", quote="beta quote text",
                source_url="https://nowhere.test",
            ),
            # quote lives in a.test but the citation credits b.test
            Citation(
                claim="y", quote="alpha quote text",
                source_url="https://b.test",
            ),
            # correct attribution — the only one that survives
            Citation(
                claim="z", quote="alpha quote text",
                source_url="https://a.test",
            ),
        ],
    )
    kept, dropped, hashes, unknown = verify_citations(draft, ev)
    assert dropped == 2
    assert unknown == 1  # only the phantom URL was never fetched
    assert kept == [draft.citations[2]]
    assert hashes == ["a" * 64]
