# ADR 0002 — Prompt Registry storage model

**Status:** accepted · **Date:** 2026-10

## Context

Prompts are the product surface of a multi-agent system: their quality
gates every downstream metric. Editing prompts in code means a deploy per
tweak and no provenance; storing them only in a SaaS (LangSmith Hub,
Langfuse Prompt Management) makes CI dependent on a network service and
hides evolution from review.

## Decision

**YAML seeds in-repo + a SQLite-backed `PromptRegistry`** (`app/services/prompt_registry.py`):

- Prompts are versioned artifacts: `prompts/<name>/vN.yaml` with template,
  declared variables, changelog.
- The registry loads seeds idempotently at startup, then owns a lifecycle:
  `draft → candidate → promoted → retired` with an A/B evaluator and a
  **metric gate** (`PROMPT_PROMOTION_EPSILON`) between candidate and
  promoted.
- Every lifecycle mutation appends to the **hash-chained audit log** —
  prompt evolution is tamper-evident, same chain family as job
  transcripts.
- Provenance rides every LLM call as `prompt_name`/`prompt_version`/
  `prompt_sha256` metadata, so traces (LangSmith or Langfuse) attribute
  generations to exact template versions.

External prompt managers stay optional mirrors — the repo registry is the
source of truth.

## Consequences

- **+** Prompt diffs are PR-reviewable; rollback is a pointer move, not a
  redeploy.
- **+** Promotion is measured (`run_ab_eval`) before it ships — a
  regressing prompt is blocked unless an admin supplies an override reason.
- **−** Two stores to keep consistent (files vs DB) — handled by
  idempotent seeding + the DB owning mutable lifecycle state.

## Amendment — overlay behind `PromptStoreProtocol` (Phase 12)

The registry no longer owns a `sqlite3` connection: it reads and writes
through `app/services/prompt_store.py` (`PromptStoreProtocol` —
`SqlitePromptStore` default, `PgPromptStore` on `psycopg` under
`STORAGE_BACKEND=postgres`). The lifecycle semantics are unchanged — YAML
seeds still win template bytes, the DB still owns status and A/B runs —
but the overlay is now a pluggable store: on Postgres the same
revision-bump re-merge means a `promote` on replica A is observed by
replica B on its next read, closing the single-host gap W7 flagged.
