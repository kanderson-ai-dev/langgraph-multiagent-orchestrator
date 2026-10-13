# Functional QA Checklist — API & Graph

Versioned end-to-end functional pass. Automated coverage lives in `tests/`;
this list is the release-time sanity sweep for flows that cross module
boundaries.

## Job lifecycle

- [ ] `POST /orchestration/reports` returns `job_id` + `202`; `X-Trace-Id`
      header present when tracing is configured.
- [ ] `GET /orchestration/reports/{id}` polls cleanly through
      queued → running → done; SSE `/events` replays node-level events.
- [ ] Budget cap breach pauses the job (`awaiting_review`,
      `escalation_reason=budget`); `fund` resume adds budget and finishes.
- [ ] Debate exhaustion pauses (`escalation_reason=debate_rounds`);
      `approve`/`edit`/`reject` resume paths behave; `reject` requires a
      reason (Phase 7).
- [ ] Stale `awaiting_review` jobs are swept to `expired` after
      `HITL_EXPIRY_HOURS` (periodic sweeper).
- [ ] Double-resume of a resolved pause returns a conflict, not a crash.
- [ ] Unknown job id returns a generic 404 (no tenant leakage).

## Guardrails & evidence

- [ ] Injection payloads in the brief are blocked pre-graph (no LLM spend).
- [ ] Scraped content is sanitized before reaching any prompt; sources are
      sealed (`evidence_manifest` with sha256 per source).
- [ ] Citations carry source-hash footers (`#<sha[:12]>`); unsupported
      citations are flagged by the reviewer path.
- [ ] Audit chain verifies via `audit_root`; tampering any transcript entry
      breaks verification (UI badge flips to INVALID).

## Prompt lifecycle

- [ ] YAML seeds load into the registry on startup (idempotent).
- [ ] Candidate creation validates template variables against the promoted
      contract.
- [ ] Promote runs the metric gate; regression blocks without override,
      override requires a reason; rollback restores prior promoted version.
- [ ] All lifecycle mutations append to the hash-chained audit log
      (tamper-evident).

## Multi-tenancy & auth

- [ ] `POST /workspaces`, member management, API-key issuance work; key is
      shown once and stored hashed.
- [ ] Role matrix: viewer=read, member=submit jobs, admin=prompt mutations +
      workspace admin; violations return generic 403/404.
- [ ] Cross-tenant reads/writes are denied for every route (jobs, reports,
      audit, prompts, dashboard, workspaces).
- [ ] API-key auth resolves workspace + role without a session.

## Observability & cost

- [ ] `/dashboard/performance` returns real aggregates only (no hardcoded
      numbers); empty state when zero jobs.
- [ ] Token usage rows come from provider-reported counts; unknown-model
      pricing fails closed.
- [ ] `GET /metrics` exposes job counters, node-latency histogram, cost
      gauges; `docs/observability/grafana-dashboard.json` imports.
- [ ] Tracing provider seam: `none`/`langsmith`/`langfuse` boot cleanly;
      LangSmith 429s degrade without breaking requests.

## Resilience

- [ ] `REDIS_URL` unset → in-memory rate limiter + event bus; set → Redis
      backends selected automatically (same contract tests pass on both via
      fakeredis).
- [ ] Scraper respects robots.txt + rate limits; search falls back to
      DuckDuckGo when no paid key.
- [ ] Health endpoint degrades gracefully without any credentials.
