# Security Model

This document maps the service's controls to the **OWASP Top 10** and the
**OWASP LLM Top 10**, with emphasis on the threat distinctive to this
architecture: **indirect prompt injection via untrusted web/document
content** gathered by the Researcher agent.

## Threat model

| Threat | Surface | Control |
|---|---|---|
| Direct prompt injection | User-supplied `Brief` (topic, audience, requirements) | `screen_brief` / `screen_input` runs *before* any LLM or external call — blocked input burns zero tokens (`app/graph/guardrails.py`) |
| Indirect prompt injection | Scraped HTML / parsed PDFs containing embedded instructions | `sanitize_untrusted` (invisible-char normalization + instruction-phrase defanging) applied in the Researcher before text reaches state; treated strictly as *data* in prompts |
| Prompt leakage | Final report echoing system prompts or reflected instructions | `screen_output` on the assembled report before it ships (OWASP LLM02) |
| SSRF | Researcher fetching arbitrary URLs | Scheme allowlist (`http`/`https`), private/loopback/link-local IP literals rejected (`app/services/scraper_client.py`) |
| Credential leakage | Logs, errors, config | `SecretStr` config, `structlog` field redaction, generic 401s, no secrets in query strings |
| Auth bypass | Job/report endpoints | JWT when `JWT_SECRET_KEY` is set; all routes scoped by `workspace_id` (multi-tenant isolation) |
| Rate abuse | `/auth/login`, job submission | Sliding-window `RateLimiter` protocol — in-memory default, Redis backend for multi-replica (`app/core/rate_limit.py`) |
| Dependency / supply chain | CI | `pip-audit`, `gitleaks`, CycloneDX SBOM + validation, CodeQL, Dependabot, SHA-pinned GitHub Actions |
| Prompt tampering | Prompt Registry templates | Lifecycle mutations are **admin-only** (RBAC) and every mutation appends to a hash-chained audit log — tampering is detectable (`PromptStore.audit_log`) |
| Promotion abuse | Registry `promote`/`rollback` | Metric-gated promotion (`run_ab_eval` + `PROMPT_PROMOTION_EPSILON`); admin overrides require an explicit `reason` that lands in the audit chain |
| Malicious candidates | `POST /prompts/{name}/candidates` | Candidate registration is admin-gated; a candidate is never promoted without measured eval results — supply ends in an auditable `draft`, never silently live |
| Report tampering | `GET /reports/{id}/audit`, `/evidence` | Transcript entries and the evidence manifest (per-source SHA-256) are sealed into the job's hash chain — `verify` re-computes the chain and flags divergence |

## RBAC

- **Roles:** `admin` (full control, including workspace membership and
  prompt lifecycle), `member` (submit/review jobs, read within the
  workspace), `viewer` (read-only).
- JWT sessions resolve `role` from the `members` table; API keys carry
  their own role from `api_keys`. The platform-admin username
  (`ADMIN_USERNAME`) bootstraps `admin` even before any membership row
  exists.
- Every route is scoped by `workspace_id`; a member of workspace A gets
  `404` (not `403`) on workspace B's resources — existence is not leaked.
- Registry lifecycle, workspace membership, API-key issuance and feedback
  export are admin-only; job submission/review require `member` or
  `admin`; reads accept `viewer`.

## Evaluation cassette policy

Recorded live eval runs (`evaluation/cassettes/*.json`) contain real
scraped HTML and LLM payloads. Before a cassette is committed:

1. It is generated with `record_live.py` — a dedicated script, never
   hand-edited.
2. `gitleaks detect` runs over the diff — caught once already (an
   Application Insights `instrumentationkey` embedded in third-party
   HTML, exempted via `.gitleaksignore` as a verified non-secret).
3. A cassette is treated as *untrusted input* in replay mode — the same
   sanitization that guards live scraping applies to recorded content.

## OWASP LLM Top 10 mapping

- **LLM01 Prompt Injection** — input guardrail on the brief; sanitization of
  all fetched content; the Supervisor/worker prompts explicitly treat
  evidence as data. Dedicated adversarial fixture:
  `tests/fixtures/adversarial_page.html`.
- **LLM02 Insecure Output Handling** — `screen_output` gates the report;
  security headers middleware; report content is rendered as text, never
  executed.
- **LLM03 Supply Chain** — `pip-audit` in CI; pinned `uv.lock`;
  `requirements.txt` generated, not hand-edited.
- **LLM05 Improper Output Handling** — citations verified against fetched
  source text (verbatim/punctuation-insensitive/≥0.85 fuzzy); unverifiable
  claims are dropped and counted.
- **LLM06 Excessive Agency** — the Supervisor's dispatch budget
  (`max_dispatches`) and the debate bound (`max_debate_rounds`) make
  termination structural; HITL escalation on exhaustion.
- **LLM07 System Prompt Leakage** — output screening; secrets never
  interpolated into prompts.
- **LLM09 Misinformation** — citation verification + human review on
  exhausted debates.

## OWASP Top 10 (classic)

- **A01 Broken Access Control** — `workspace_id` scoping on every job
  read/write; no cross-tenant visibility.
- **A02 Cryptographic Failures** — bcrypt for passwords, signed JWTs,
  `SecretStr` for all secrets.
- **A03 Injection** — parameterized SQL (aiosqlite), no raw interpolation.
- **A05 Security Misconfiguration** — security headers
  (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`),
  non-root Docker image (Phase 12).
- **A07 Identification/Auth Failures** — generic 401 responses (never
  distinguish "user not found" vs "wrong password"), bcrypt, short-lived JWT.
- **A09 Logging Failures** — structured logs with automatic secret redaction.

## Known limitations

- In-memory rate limiter does not coordinate across replicas (documented in
  code and README).
- SSRF guard rejects IP *literals*; DNS-rebinding protection (resolving
  hostnames to private ranges) is a documented next step.
- robots.txt fetch failures default to *allow* with a log line — trade-off
  documented in `scraper_client.py`.
