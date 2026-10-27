# ADR 0005 — HMAC-signed audit roots

**Status:** accepted · **Date:** 2026-10

## Context

Every job ships a tamper-evident transcript: each `AgentMessage` joins a
SHA-256 hash chain (`app/services/audit.py`), and the chain tip
(`audit_root`) is stored with the job result so `GET /reports/{id}/audit`
can recompute and compare. Hash-only integrity has a documented hole: an
attacker with write access to the SQLite file can rewrite the transcript
*and* recompute a self-consistent chain — the tip verifies and the
tampering is invisible.

## Decision

**Seal the chain tip with HMAC-SHA256 when `AUDIT_SIGNING_KEY` is set.**

- `sign_root` MACs the canonical string `workspace_id|job_id|root`, so a
  signature cannot be transplanted to another job or tenant.
- The signature is stored inside `result_json` as `audit_signature` — no
  schema migration; legacy unsigned jobs simply carry none.
- `GET /reports/{id}/audit` answers honestly in all three states:
  `signature_valid: true|false` when a key and signature exist,
  `signature_valid: null` (with `"signed": false`) when the deployment
  has no key — hash-chain integrity only, said so in the response.
- The prompt-lifecycle audit chain (`prompt_events` in the registry)
  uses the same mechanism: entries are hash-chained always and signed
  when `PromptRegistry.load(signing_key=...)` is given the same env key.
- Verification is constant-time (`hmac.compare_digest`).

## Consequences

- **+** A DB-only attacker can still forge a *consistent* hash chain but
  cannot forge the MAC — `signature_valid` flips false and the forgery is
  caught. Verified by a regression test that recomputes the chain after
  tampering.
- **+** Zero-config degradation: no key → everything works, reported
  honestly as unsigned. CI and offline tests never need the key.
- **−** The key must live outside the database (env var / secret
  manager) — an attacker who steals both can forge signatures. This is
  documented, not mitigated.
- **−** No key rotation (`AUDIT_SIGNING_KEY_ID` is the planned seam for a
  `kid → key` map) and no external timestamping/anchoring — a chain can
  be replaced wholesale by anyone holding the key. Recorded as next steps
  in SECURITY.md "Known limitations".
  *(Superseded — see Amendment below.)*

## Alternatives rejected

- **External timestamping / transparency log** — stronger non-repudiation
  but adds a network dependency to every seal; wrong trade for a single-
  node service today. The `kid`-keyed map + anchor can be layered on later
  without changing the signed payload.
- **Signing every chain entry** — per-entry MACs catch mid-chain edits,
  but the chain is recomputed wholesale at verify time anyway; signing
  the tip already authenticates the full history and keeps the write path
  O(1).
- **Asymmetric signatures** — lets verifiers run without the secret, but
  this service is both signer and verifier; a second key type buys
  nothing until a third-party verifier exists.

## Amendment — key ids, rotation, and a local anchor (delivered)

**Status:** accepted · **Date:** 2026-11

The two "no rotation / no anchoring" limitations above are now closed:

- **Key ids.** `sign_root` emits `key_id:hmac` where `key_id` is the first
  8 hex of `sha256(key)` — non-secret, stored beside the signature. The
  same format signs prompt-lifecycle events.
- **Rotation.** `AUDIT_SIGNING_PREVIOUS_KEYS` (comma-separated, env-only)
  builds a `key_id → key` verification ring: only `AUDIT_SIGNING_KEY`
  mints new signatures, retired keys still verify old ones. An unknown
  `key_id` yields `signature_valid: false` — a clean failure, never an
  exception. Legacy prefix-free signatures are tried against every ring
  key for backward compatibility.
- **Local anchor.** Each sealed job appends one JSONL line
  (`AUDIT_ANCHOR_PATH`, default `data/audit_anchor.jsonl`) binding
  `job_id → audit_root`. The file is independent of the jobs DB —
  copyable off-box — and `GET /reports/{id}/audit` reports
  `anchor_match` (`true`/`false`/`null` when the job predates the log).
  `scripts/audit_anchor.py` prints the latest root for owner publication
  (Git tag, transparency log) and `--check` diffs every anchor against
  the DB.
- **Honest unsigned.** `signature_valid: null` renders "unsigned" on the
  console badges and the PDF/DOCX footer — never implied-valid.

Still open: real third-party timestamping (RFC 3161 TSA / OpenTimestamps)
over the anchor root — the anchor file is the stable artifact a TSA would
stamp. Recorded as the next step in SECURITY.md.

## Amendment — RFC 3161 timestamp anchoring (delivered)

**Status:** accepted · **Date:** 2026-10

The "external timestamping" limitation above is now closed as an
optional deployment feature:

- **Setting.** `AUDIT_ANCHOR_TSA_URL` (empty = disabled). When set, each
  sealed job's `audit_root` is stamped by the configured TSA via the
  standard Time-Stamp Protocol.
- **What is stamped.** The message is `bytes.fromhex(audit_root)` — the
  raw 32-byte digest — so offline verification recomputes
  `sha256(root_bytes)` and compares it to the token's `messageImprint`.
- **Where receipts live.** A sidecar log `<anchor>.tsa.jsonl` next to
  the anchor file; the primary JSONL stays append-only and unchanged.
- **Failure semantics.** Stamping runs as a bounded background task
  (3 attempts, exponential backoff); a TSA outage is logged and never
  fails or delays a sealed job.
- **Offline verification.** `scripts/audit_anchor.py --verify`
  re-verifies every stored receipt: signature over the token, imprint
  matches the root, genTime extracted. No network.
- **Trust model.** Verification uses the TSA certificate embedded in
  the token (requested with `certReq=true`) chained against the public
  CA bundle (certifi); a self-signed TSA acts as its own root. The
  receipt records the signer certificate's sha256 fingerprint so a
  different TSA key is detectable. A timestamp proves the digest existed
  at a time under a TSA key — not truthfulness of the report, and not
  that the operator trusts that particular TSA; deployments pin trust by
  choosing `AUDIT_ANCHOR_TSA_URL` deliberately.
- **Optional dependency.** `rfc3161-client` ships under the
  `anchoring` extra only — the core install and all offline tests work
  without it.
