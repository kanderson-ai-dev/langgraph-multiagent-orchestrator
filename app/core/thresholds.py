"""Single source of truth for the versioned EDD quality thresholds.

The same numbers gate the offline eval (``evaluation/run_eval.py``),
prompt-candidate promotion (``prompt_eval.promotion_gate``) and the
Reviewer's per-job citation check — one dict, imported everywhere, so a
threshold can never drift between the gate and the runtime.
"""

EDD_THRESHOLDS: dict[str, float] = {
    "citation_support_rate": 0.90,
    "rubric_score_avg": 4.0,
    "judge_score_avg": 4.0,
    "debate_convergence_rate": 0.85,
    "blocked_rate": 1.0,
    # Share of cited claims a judge rates "yes" (the claim is literally
    # supported by its quoted passage) — averaged over quality cases.
    "claim_support_rate_avg": 0.85,
}
