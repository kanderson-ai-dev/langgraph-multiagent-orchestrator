# Screenshots

Captures referenced from the project `README.md`. Keep filenames stable —
the README links them by name.

| File | What it shows | README section |
|---|---|---|
| `demo.gif` | End-to-end demo — brief submit → live transcript → HITL modal → sealed report → workspace costs | "Demo" (top of README) |
| `report-audit-badge.png` | Console with a finished job — Final report card, `audit ✓` seal, Download PDF, team chips, Recent jobs, Workspace panel | "Auditability" |
| `swagger-docs.png` | `/docs` — auto-generated OpenAPI surface | "API" |
| `edd-scorecard.png` | Terminal output of `python -m evaluation.run_eval` with all gates PASS | "Evaluation" |
| `langsmith-experiment-summary.png` | LangSmith experiment — feedback bars + P50/P99 latency | "Evaluation" |
| `langsmith-experiment-detail.png` | LangSmith experiment rows — per-case evaluator scores (4 green cases + red `blocked-injection` row) | "Evaluation" |

The live-transcript, HITL-modal and workspace stills are intentionally absent —
`demo.gif` already covers them in motion, and stills would duplicate it.

## Regenerating

- `demo.gif` → `uv run --with playwright python scripts/record_demo.py`
- `report-audit-badge.png` / `swagger-docs.png` / `edd-scorecard.png` →
  `uv run --with playwright python scripts/capture_screenshots.py`
  (server running locally, at least one `done` job in Recent jobs)
- LangSmith PNGs → manual captures from the LangSmith UI after running
  `python -m evaluation.run_eval --langsmith` with `LANGCHAIN_API_KEY` set.
