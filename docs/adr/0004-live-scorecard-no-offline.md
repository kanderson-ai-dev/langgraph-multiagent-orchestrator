# ADR 0004 — Why the live scorecard has no offline mode

**Status:** accepted · **Date:** 2026-10

## Context

Two evaluation surfaces exist: the **offline EDD scorecard**
(`run_eval`, deterministic stub LLM + fixture tools — the CI gate) and the
**live experiment** (`--langsmith-live` / `--langfuse-live`, real model +
real search — the telemetry). A tempting shortcut: let the live surface
fall back to stub mode when credentials are missing, so a screenshot
always exists.

## Decision

**No offline fallback for live surfaces.** `--langsmith-live` /
`--langfuse-live` require real credentials and publish only measured
results; the offline scorecard is a separate artifact with its own gates.

Rationale: a stub "live" run is indistinguishable at a glance from a real
one in a dashboard screenshot — exactly the dishonesty the EDD discipline
exists to prevent. When LangSmith quota was exhausted during development,
the answer was a provider seam + Langfuse fallback publishing *real*
traces, not a prettier stub run.

## Consequences

- **+** Every published experiment row is a real measurement — the
  `blocked-injection` zeros and the weak convergence numbers are honest
  signal, not calibration.
- **+** CI never secretly burns quota; offline gates never secretly become
  live ones.
- **−** A broken credential shows up as "no experiment", not a green one —
  that absence is the correct signal.
