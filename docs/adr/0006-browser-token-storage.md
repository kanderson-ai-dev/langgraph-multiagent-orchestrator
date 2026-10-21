# ADR 0006 — Browser token storage for the operator console

**Status:** accepted · **Date:** 2026-10 · amended 2026-10 (opt-in
persistence removed, JWT lifetime shortened)

## Context

The console (`/console`) needs a credential to call the API. Three
options: cookie session, `localStorage` token, or `sessionStorage` token.
The earlier implementation kept the JWT in `localStorage` — persistent
across restarts, readable by any script running on the origin (XSS =
token theft), and **never invalidated server-side** — logout was purely
client-side, so a copied token stayed valid until `exp`.

## Decision

**Session-scoped storage only + real logout + short JWT lifetime.**

- Tokens live in `sessionStorage` (cleared when the tab closes). The
  original decision shipped a "Remember me" checkbox that opted into
  `localStorage`; on review the residual risk (an XSS-readable token
  persisting past the operator's intent) outweighed the convenience, so
  the opt-in was **removed** — `setToken` now only ever *clears*
  `localStorage` so tokens persisted by older releases stop working.
- JWT lifetime was shortened (`JWT_EXPIRE_MINUTES` default 60 → 30) to
  bound the window a copied token stays usable — there is no refresh
  token, so expiry is the primary containment.
- Every JWT carries a unique `jti`; `POST /auth/logout` inserts it into
  `revoked_tokens` (keyed by `jti`, kept until its own `exp`), and
  `get_context` rejects revoked ids — sign-out is enforced server-side,
  not just by deleting browser state. Logout is idempotent and returns
  `{"revoked": false}` for already-invalid or API-key credentials.
- API keys (`orx_…`) are *not* sessions — they are revoked through
  `DELETE /workspaces/{id}/api-keys/{key_id}` and never stored in
  browser storage by the console.

## Consequences

- **+** Closing the tab ends the session, always; a stolen token has a
  server-side kill switch *and* a short expiry.
- **+** Revocation survives redeploys (SQLite row, not process memory).
- **−** `sessionStorage` is still reachable by injected script — XSS on
  the origin is still token theft while the tab lives. Mitigated in
  depth by the strict per-surface CSP (`/console` is self-only), not
  eliminated.
- **−** Operators re-authenticate after 30 idle minutes instead of
  staying signed in across restarts — accepted trade for a
  single-operator console.
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
- **`localStorage` (opt-in or only)** — convenient persistence, but every
  session outlives the operator's intent and the token stays
  XSS-readable indefinitely.
- **Refresh-token rotation** — correct at consumer-app scale, but adds a
  second credential type and rotation state for a single-operator console
  whose tokens last `JWT_EXPIRE_MINUTES` anyway.
