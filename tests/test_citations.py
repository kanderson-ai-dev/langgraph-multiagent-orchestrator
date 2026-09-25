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
