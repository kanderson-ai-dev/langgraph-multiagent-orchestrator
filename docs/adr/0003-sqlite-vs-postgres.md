# ADR 0003 — SQLite JobStore vs Postgres

**Status:** accepted · **Date:** 2026-10

## Context

Multi-tenant job state needs durable, concurrent storage. Postgres is the
default answer for "serious" backends — but it makes `docker compose up`
heavier, complicates CI, and this service's write rate (a few jobs per
minute, not per second) doesn't demand it.

## Decision

**SQLite via `aiosqlite` today; Postgres behind a `JobStore` protocol as
the explicit next step.**

- `JobStore`, `UsageStore`, `FeedbackStore`, `PromptRegistry`, `CacheStore`
  all take a `db_path` and manage their own DDL — swap is mechanical when
  scale requires it.
- Checkpoints already use `AsyncSqliteSaver` — the HITL resume path
  benefits from the same WAL-mode durability.
- The *actually* shared-state pieces were decoupled first: `RateLimiter`
  and `EventBus` are protocols with optional **Redis** backends
  (`REDIS_URL` auto-selects) — the bottleneck for horizontal replicas is
  rate-limit/event distribution, not the job table.

## Consequences

- **+** Zero-infra dev loop and CI; the whole test matrix runs offline
  with `tmp_path` databases.
- **+** Embedded DB = backup = `cp` of a file; demo/portfolio friendly.
- **−** Single-writer limits horizontal scale — mitigated by WAL mode and
  the documented Postgres migration path (not half-implemented). Every
  store connection goes through `app/services/sqlite_util.py`, which sets
  `journal_mode=WAL` (readers never block on an uncommitted writer),
  `busy_timeout=5000` (writers queue instead of erroring instantly),
  `synchronous=NORMAL` (safe under WAL; a full OS/power loss can roll
  back the last commit) and `foreign_keys=ON`. Verified: a test hammers
  one file with 25 interleaved writers and none raise
  `OperationalError`. What WAL does **not** fix: still one writer at a
  time, still single-host — Postgres remains the scale-out step.
  LangGraph's own `AsyncSqliteSaver` enables WAL on the checkpoints file.
- **−** No row-level locking across replicas — acceptable until multi-node
  deploys arrive.
- **Multi-replica honesty (2026-10):** with `REDIS_URL` set, replicas can
  safely share a rate limiter and event bus; sharing the SQLite *file* is
  single-host only and is supported for the prompt registry (reads re-merge
  the DB overlay whenever `prompt_events` grows — a promote in process A is
  visible in B without restart) and for A/B run records (persisted in
  `prompt_ab_runs`, so `GET /ab-runs/{id}` answers after a restart).
  Process-local state that must **not** be relied on across replicas:
  `JobRunner` task handles/last-values/trace-ids (freed on terminal job
  states; a resumed job must run on the same process that submitted it),
  the `ab_runs` hot cache (100 entries / 1 h — the DB row is the truth),
  and in-memory rate limiting (use `REDIS_URL`). Job rows themselves stay
  single-writer — Postgres remains the real multi-node step.
