# Screenshots

Captures referenced from the project `README.md`. Keep filenames stable —
the README links them by name.

| File | What to capture | README section |
|---|---|---|
| `landing-brief.png` | Landing page `/` — brief form with report type, budget cap and client context | "What this is" / Quick Start |
| `console-live-debate.png` | `/console` mid-run — live SSE transcript showing Supervisor dispatches and the Writer↔Reviewer debate | "The loop" |
| `console-hitl-modal.png` | HITL modal — approve / edit / reject / fund actions on a paused job | "Human-in-the-loop" |
| `report-audit-badge.png` | Final report card with the `audit ✓ <hash>` badge and PDF download | "Auditability" |
| `console-recent-jobs.png` | Sidebar "Recent jobs" + Workspace panel (cost per role, tokens, latency) | "Cost governance & observability" |
| `swagger-docs.png` | `/docs` — auto-generated OpenAPI surface | "API" |
| `edd-scorecard.png` | Terminal output of `python -m evaluation.run_eval` with all gates PASS | "Evaluation" |
| `langsmith-experiment-summary.png` | LangSmith experiment `edd-offline-29d50610` — feedback bars + P50/P99 latency on `langgraph-orchestrator-edd` | "Evaluation" |
| `langsmith-experiment-detail.png` | LangSmith experiment rows — per-case evaluator scores (4 green cases + red `blocked-injection` row) | "Evaluation" |
| `langsmith-trace.png` | LangSmith Tracing — one job's span tree (supervisor → workers) | "Observability" |
| `demo.gif` | End-to-end demo — brief submit → live transcript → HITL → sealed report | "Demo" (top of README) |

Capture notes:

- Prefer dark theme, full-width screenshots cropped to the relevant area.
- The live-debate shot is best taken while a job is mid-run (open the job
  from "Recent jobs" right after submitting).
- For `console-hitl-modal.png`, submit a brief with a tiny budget
  (`0.01`) — the job escalates to review with `budget_exceeded` and the
  modal shows the "Fund & continue" action.
