# Functional QA runs — UI matrix

Recorded by `tests/test_frontend_qa_matrix.py` (offline StubLLM
+ fixture tools; stub costs are chars/4 estimates, reported as
stub — live numbers come only from `scripts/qa_live_matrix.py`).

| # | Case | Expected | Observed | Cost | Wall (s) | Shot |
|---|---|---|---|---|---|---|
| 1 | happy-path | done | done | $0.0002 | 3.4 | `docs/ui-qa/runs/01-happy-path.png` |
| 2 | debate-rounds | done | done | $0.0005 | 3.4 | `docs/ui-qa/runs/02-debate-rounds.png` |
| 3 | parallel-fanout | done | done | $0.0002 | 3.4 | `docs/ui-qa/runs/03-parallel-fanout.png` |
| 4 | minimal-brief | done | done | $0.0002 | 3.4 | `docs/ui-qa/runs/04-minimal-brief.png` |
| 5 | unicode-long | done | done | $0.0002 | 3.5 | `docs/ui-qa/runs/05-unicode-long.png` |
| 6 | spanish-brief | done | done | $0.0002 | 3.3 | `docs/ui-qa/runs/06-spanish-brief.png` |
| 7 | budget-fund | done | done | $0.0002 | 3.6 | `docs/ui-qa/runs/07-budget-fund.png` |
| 8 | hitl-approve | done | done | $0.0005 | 3.6 | `docs/ui-qa/runs/08-hitl-approve.png` |
| 9 | hitl-edit | done | done | $0.0005 | 4.0 | `docs/ui-qa/runs/09-hitl-edit.png` |
| 10 | hitl-reject | failed | failed | $0.0004 | 3.4 | `docs/ui-qa/runs/10-hitl-reject.png` |
| 11 | prompt-injection | blocked/failed | blocked | $0.0000 | 3.3 | `docs/ui-qa/runs/11-prompt-injection.png` |
| 12 | tool-failure | failed | failed | $0.0003 | 3.5 | `docs/ui-qa/runs/12-tool-failure.png` |
| 13 | back-to-back | done | done | $0.0002 | 3.6 | `docs/ui-qa/runs/13-back-to-back.png` |
| 14 | prompt-version | done | done | $0.0001 | 3.7 | `docs/ui-qa/runs/14-prompt-version.png` |

## Aggregate gate

- 14/14 runs matched their expected outcome.
- Per-run asserts executed: terminal state, report render, cost parity UI↔API, elapsed parity, artifact text scan (incl. during HITL pause), mid-run + post-completion reload persistence, zero console errors, screenshot saved.
- Stub-mode quality numbers (citation/rubric) are covered by the offline EDD scorecard, not this matrix; live numbers live in the Langfuse experiment record.

## Live pass — `scripts/qa_live_matrix.py` (real OpenAI + search/scrape)

Budget-capped manual pass; `auto_review` resolves HITL pauses like an
operator. Recorded 2026-10-15, spend $0.0141 total (cap $0.50).

| Case | Expected | Observed | Cost | Wall (s) | Judge | Notes |
|---|---|---|---|---|---|---|
| live-happy | done/awaiting | done | $0.0088 | 95.3 | 4.0 | hitl_resumes=1 (operator approve after rounds) |
| live-hitl | done | done | $0.0053 | 70.5 | 4.0 | hitl_resumes=1 |
| live-injection | blocked | blocked | $0.0000 | 0.0 | — | 0 tokens spent pre-block |

Honest note: both delivered cases converged only via operator resume
(hitl_resumes=1) — live debate convergence remains below the 0.85 gate,
consistent with the Langfuse experiment record.
