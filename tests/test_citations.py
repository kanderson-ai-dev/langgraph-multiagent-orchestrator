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


def test_curly_quotes_and_typographic_apostrophe() -> None:
    source = "The bank’s deployment cut resolution time by 40 % across pilots."
    quote = "the bank's deployment cut resolution time by 40 % across pilots"
    assert citation_supported(quote, source)


def test_em_dash_and_nbsp_and_ellipsis() -> None:
    source = (
        "Revenue grew 12\u00a0% year-over-year — driven by edge "
        "deployments\u2026 with more to come."
    )
    quote = "revenue grew 12 % year-over-year driven by edge deployments"
    assert citation_supported(quote, source)


def test_unicode_punctuation_does_not_save_a_wrong_number() -> None:
    source = "Costs fell 12 % — the report says."
    assert not citation_supported("costs fell 21 %", source)


def test_fullwidth_forms_fold() -> None:
    source = "Ｒｅｌｅａｓｅ ｖ２ launched in June."
    assert citation_supported("release v2 launched in june", source)


def test_ellipsis_span_accepted_when_in_order() -> None:
    source = (
        "Adoption doubled in 2024 as tooling matured. Several unrelated "
        "sentences sit between the two halves of this evidence. Procurement "
        "teams report audit preparation time fell by 30 %."
    )
    quote = "adoption doubled in 2024 as tooling matured … audit preparation time fell by 30 %"
    assert citation_supported(quote, source)


def test_ellipsis_out_of_order_rejected() -> None:
    source = "Alpha first claim here. Much later: second claim there."
    assert not citation_supported(
        quote="second claim there … alpha first claim here", source_text=source
    )


def test_ellipsis_short_segment_rejected() -> None:
    source = "Costs fell sharply. Later: adoption rose quickly across the board."
    assert not citation_supported("costs fell … rose quickly across the board", source)


def test_distant_sentences_without_ellipsis_rejected() -> None:
    source = (
        "Adoption doubled in 2024 as tooling matured and buyers returned. "
        "Fifty unrelated words now separate the halves of this evidence so "
        "no window can contain both claims at once, on purpose, really. "
        "Procurement teams report audit preparation time fell by 30 %."
    )
    quote = (
        "adoption doubled in 2024 as tooling matured and buyers returned "
        "audit preparation time fell by 30 %"
    )
    assert not citation_supported(quote, source)


def test_markdown_link_and_emphasis_stripped() -> None:
    source = "The platform reports 99.95 % uptime across regions."
    assert citation_supported("[the platform reports](https://x) **99.95 % uptime**", source)


def test_mutated_digit_breaks_accepted_quote_seeded() -> None:
    """Property: mutating any digit of an accepted quote makes it rejected."""
    import random

    source = "Revenue grew 12 % in 2024 and churn fell to 3 %."
    quote = "revenue grew 12 % in 2024 and churn fell to 3 %"
    assert citation_supported(quote, source)
    rng = random.Random(20261026)
    positions = [i for i, ch in enumerate(quote) if ch.isdigit()]
    for pos in positions:
        mutated = list(quote)
        mutated[pos] = rng.choice("0123456789")
        if mutated[pos] == quote[pos]:
            mutated[pos] = str((int(quote[pos]) + 1) % 10)
        assert not citation_supported("".join(mutated), source), pos
