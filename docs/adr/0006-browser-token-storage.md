# ADR 0006 — Browser token storage for the operator console

**Status:** accepted · **Date:** 2026-10

## Context

The console (`/console`) needs a credential to call the API. Three
options: cookie session, `localStorage` token, or `sessionStorage` token.
The earlier implementation kept the JWT in `localStorage` — persistent
across restarts, readable by any script running on the origin (XSS =
token theft), and **never invalidated server-side** — logout was purely
client-side, so a copied token stayed valid until `exp`.

## Decision

**Session-scoped storage by default + opt-in persistence + real logout.**

- Tokens live in `sessionStorage` (cleared when the tab closes); the
  "Remember me" checkbox in the sign-in modal opts into `localStorage`
  for operators who want persistence.
- Every JWT carries a unique `jti`; `POST /auth/logout` inserts it into
  `revoked_tokens` (keyed by `jti`, kept until its own `exp`), and
  `get_context` rejects revoked ids — sign-out is enforced server-side,
  not just by deleting browser state. Logout is idempotent and returns
  `{"revoked": false}` for already-invalid or API-key credentials.
- API keys (`orx_…`) are *not* sessions — they are revoked through
  `DELETE /workspaces/{id}/api-keys/{key_id}` and never stored in
  browser storage by the console.

## Consequences

- **+** Closing the tab ends the session by default; a stolen token has a
  server-side kill switch; "Remember me" makes the persistence trade an
  explicit operator choice.
- **+** Revocation survives redeploys (SQLite row, not process memory).
- **−** `sessionStorage`/`localStorage` are still reachable by injected
  script — XSS on the origin is still token theft. Mitigated in depth by
  the strict per-surface CSP (`/console` is self-only), not eliminated.
- **−** The revoked-token table grows until `exp` sweeps; bounded by
  `JWT_EXPIRE_MINUTES` × login rate — acceptable at this scale.

## Alternatives rejected

- **httpOnly cookies** — removes JS access to the token entirely, but
  reintroduces CSRF surface (needs SameSite + CSRF tokens on every
  mutating route), complicates the SSE `fetch` path less than it
  complicates the API's shared bearer contract (same endpoints serve
  `orx_` keys), and buys little while the console CSP already denies
  third-party script. Revisit if the console ever serves user-generated
  markup.
- **`localStorage` only** — the status quo ante: convenient, but every
  session outlives the operator's intent and there was no server-side
  revocation to shorten it.
- **Refresh-token rotation** — correct at consumer-app scale, but adds a
  second credential type and rotation state for a single-operator console
  whose tokens last `JWT_EXPIRE_MINUTES` anyway.
