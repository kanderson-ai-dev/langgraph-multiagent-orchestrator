# EDD Trend History

Append-only history in `history.jsonl`; this file is regenerated
from it — do not hand-edit.

| Run (UTC) | SHA | mode | prompts | model | judge_model | citation | rubric | judge | converge | blocked | cost_avg | p50_s | p95_s | base_support | base_cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-13T17:00:38.738551+00:00 | 21452e2 | offline (stub LLM + fixture tools) | researcher@v1,reviewer@v1,writer@v1 | gpt-4o-mini | gpt-4o | 1.000 | 4.50 | 4.50 | 1.000 | 1.000 | 0.000189 | 0.02 | 0.03 | 0.500 | 0.000108 |
| 2026-10-13T17:28:09.949205+00:00 | 21452e2 | offline (stub LLM + fixture tools) | judge@v1,researcher@v1,reviewer@v1,writer@v1 | gpt-4o-mini | gpt-4o | 1.000 | 4.50 | 4.50 | 1.000 | 1.000 | 0.000189 | 0.03 | 0.03 | 0.500 | 0.000108 |

## Latest case results

- `rag-saas` — status=done, support=1.00, score=4.5, judge=4.5, rounds=1, cost=$0.000190 | baseline: support=0.50, score=4.5, cost=$0.000108
- `eu-ai-act` — status=done, support=1.00, score=4.5, judge=4.5, rounds=1, cost=$0.000188 | baseline: support=0.50, score=4.5, cost=$0.000107
- `pricing` — status=done, support=1.00, score=4.5, judge=4.5, rounds=1, cost=$0.000188 | baseline: support=0.50, score=4.5, cost=$0.000107
- `multiagent` — status=done, support=1.00, score=4.5, judge=4.5, rounds=1, cost=$0.000189 | baseline: support=0.50, score=4.5, cost=$0.000108
- `blocked-injection` — status=blocked, support=0.00, score=0.0, judge=0.0, rounds=0, cost=$0.000000 | baseline: support=0.00, score=4.5, cost=$0.000100
