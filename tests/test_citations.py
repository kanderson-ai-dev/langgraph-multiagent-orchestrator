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


# ─── critical-token check: a high fuzzy ratio must not launder a changed
# number, negation, year, unit/currency, or entity ────────────────────────────


def test_changed_number_is_unsupported() -> None:
    src = "Acme reported that revenue grew 12% in 2023 across all regions."
    # One digit swapped — the ratio still clears 0.85 without the check.
    assert not citation_supported("revenue grew 21% in 2023", src)
    assert citation_supported("revenue grew 12% in 2023", src)


def test_changed_negation_is_unsupported() -> None:
    src = "The regulator did not approve the merger in the last quarter."
    assert not citation_supported("the regulator did approve the merger", src)
    assert citation_supported("the regulator did not approve the merger", src)


def test_changed_year_is_unsupported() -> None:
    src = "The protocol was finalized in 2023 after two years of drafts."
    assert not citation_supported("the protocol was finalized in 2024", src)


def test_changed_currency_is_unsupported() -> None:
    src = "The contract was valued at €5 million last year."
    assert not citation_supported("the contract was valued at $5 million", src)


def test_changed_unit_is_unsupported() -> None:
    src = "The dataset weighs 5 GB uncompressed on disk."
    assert not citation_supported("the dataset weighs 5 TB uncompressed", src)


def test_changed_entity_is_unsupported() -> None:
    src = (
        "In the benchmark, LangGraph outperformed the baseline on every "
        "multi-step planning task."
    )
    assert not citation_supported(
        "in the benchmark, LangChain outperformed the baseline", src
    )
    assert citation_supported(
        "in the benchmark, LangGraph outperformed the baseline", src
    )


def test_benign_variation_still_supported() -> None:
    """Case, whitespace, punctuation and small paraphrase drift remain
    supported — the check only vetoes *critical* token changes."""
    src = (
        "LangGraph is a framework for building stateful, multi-agent "
        "applications with LLMs."
    )
    assert citation_supported(
        "langgraph is a framework for building stateful multi agent applications",
        src,
    )
    assert citation_supported(
        "LangGraph is a framework for building stateful, multi-agent applications",
        src,
    )


def test_number_outside_best_window_still_checks() -> None:
    """The critical token must be inside the matched window — a source
    that mentions the right number elsewhere does not rescue the quote."""
    src = (
        "Revenue reached 21 billion last year. Separately, analysts noted "
        "that revenue grew 12% in 2023 across all regions."
    )
    assert not citation_supported("revenue grew 21% in 2023", src)
