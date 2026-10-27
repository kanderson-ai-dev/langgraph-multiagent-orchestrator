# ADR 0007 — Extractive citations and in-loop claim adjudication

**Status:** accepted · **Date:** 2026-10-27

## Context

The original Writer emitted free-quote citations `{claim, quote,
source_url}`. Two failure modes measured live: (a) the model paraphrases
the "quote", so verbatim quote-in-source verification rejects real
citations; (b) the model attaches a topically related URL whose text does
not state the claim, so *citation support* (quote exists) stays high
while *claim support* (evidence entails the claim) stays low — the gate
was measuring the wrong thing.

## Decision

**`citation_mode = extractive` is the default.** The Writer cites passage
ids (`S0-P2`) over a pre-ranked, prose-filtered passage index; the system
resolves each id to verbatim evidence text and its source URL. The model
never retypes a quote, so quote fidelity is structural, not prompt-based.
`free_quote` remains available behind the setting.

**Claim support is adjudicated inside the debate loop.** Each citation's
claim is judged against its (verbatim) quote by an independent LLM judge
— three ballots, majority verdict. Deterministic short-circuits skip the
judge where the answer is open-and-shut: verbatim claims auto-accept;
operative figures absent from the quote auto-flag `partly`. Reviewer
feedback to the Writer names each flagged claim's repair class — TRIM
(sentence narrows to what the quote states) or DELETE (no passage states
the core) — computed from the same coverage metric the Writer's remap
uses.

## Consequences

- **+** Citation support became structurally meaningful (1.000 across the
  proof runs) and claim support measures what the gate intends.
- **+** The Writer's unsupported claims are trimmed or deleted — never
  silently re-cited to a different source.
- **+** Judge cost stays bounded (~3 ballots/claim, skipped on
  deterministic cases); the judge prompt was calibrated against a
  hand-labeled set (pooled 87.9% agreement, bar 85%).
- **−** `partly` verdicts are tolerated up to the 0.85 claim-support bar;
  residual `partly` claims are disclosed in run artifacts, not hidden.
- The pre-upgrade VCR cassettes were re-recorded against the final stack
  (extractive prompt shapes) in Phase 8.
