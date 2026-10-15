# Screenshots

Captures referenced from the project `README.md`. Keep filenames stable —
the README links them by name.

| File | What it shows | README section |
|---|---|---|
| `demo.gif` | End-to-end demo — brief submit → live transcript → HITL modal → sealed report → workspace costs | "Demo" (top of README) |
| `report-audit-badge.png` | Console with a finished job — Final report card, `audit ✓` seal, Download PDF, team chips, Recent jobs, Workspace panel | "Auditability" |
| `swagger-docs.png` | `/docs` — auto-generated OpenAPI surface | "API" |
| `edd-scorecard.png` | Terminal output of `python -m evaluation.run_eval` with all gates PASS | "Evaluation" |

Live-experiment captures live under `docs/eval/` (Langfuse is the active
backend — `docs/eval/why-langfuse.md`):

| File | What it shows |
|---|---|
| `docs/eval/langfuse-experiment-runs.png` | Experiment runs table — one run per commit on the golden dataset: item count, error count, latency, token cost |
| `docs/eval/langfuse-ab-compare.png` | Prompt A/B — writer v2 vs promoted v1 scores per metric and per case |

The live-transcript, HITL-modal and workspace stills are intentionally absent —
`demo.gif` already covers them in motion, and stills would duplicate it.

## Regenerating

- `demo.gif` → `uv run --with playwright python scripts/record_demo.py`
- `report-audit-badge.png` / `swagger-docs.png` / `edd-scorecard.png` →
  `uv run --with playwright python scripts/capture_screenshots.py`
  (server running locally, at least one `done` job in Recent jobs)
- Langfuse PNGs (`docs/eval/`) → manual captures from the Langfuse UI after
  `python -m evaluation.run_eval --langfuse-live` or
  `uv run python scripts/run_prompt_ab_live.py --pin <name>=<v>`
  (`TRACING_PROVIDER=langfuse`, keys in `.env`).
