# UI QA Checklist — Operator Console

Versioned manual/automated QA pass for `/console`. Run before any release
that touches `frontend/console/`. Automated items are covered by
`tests/test_frontend_e2e.py` (Playwright); the rest are visual checks via
`scripts/capture_screenshots.py` or a live session.

## Layout & responsiveness

- [ ] 375 px (mobile): no horizontal scroll; form stacks; tabs wrap or fit;
      report card readable without zooming. *(automated:
      `test_console_responsive`)*
- [ ] 768 px (tablet): no horizontal scroll on all four tabs; two-column
      jobs layout collapses gracefully. *(automated)*
- [ ] 1280 px (desktop): all four tabs render without overflow; Recent jobs
      sidebar and job detail side-by-side. *(automated)*
- [ ] Toasts appear top-right and auto-dismiss.
- [ ] Long report markdown scrolls inside the card, not the page.

## Tabs

- [ ] Four tabs present: Jobs, Prompt Registry, Performance & Cost, Audit.
      `role="tablist"`/`tab`/`tabpanel` wired with `aria-selected` and
      `aria-controls`.
- [ ] Switching tabs hides/shows panels; lazy data loads on first open.

## Jobs tab

- [ ] Brief form fields have labels; validation errors surface inline.
- [ ] Submit creates a job, shows live SSE transcript (`aria-live="polite"`
      on `#feed`), status chip updates.
- [ ] Debate rounds and escalations appear in the transcript with kind tags.
- [ ] `awaiting_review` opens the HITL modal showing only the server's
      `allowed_actions` (e.g. fund for budget pauses, approve/edit/reject
      for debate pauses).
- [ ] Completed job renders report markdown, prompt-version chips, sealed
      evidence hash chips, audit badge, PDF link.
- [ ] `?job=<id>` deep link restores the job on reload.

## Prompt Registry tab

- [ ] Prompt names list; selecting one prefills the template and lists
      versions with status chips (draft/candidate/promoted/retired).
- [ ] Diff viewer renders added/removed lines between two versions.
- [ ] Candidate registration re-renders the version table (regression:
      previously required reselect).
- [ ] A/B run shows score deltas; promote shows metric-gate outcome —
      blocked gate demands an override reason; override + rollback work.
      *(automated: `test_prompt_registry_tab_flow`)*

## Performance & Cost tab

- [ ] Empty state shows a `.empty-note` banner when no jobs exist.
      *(automated: `test_performance_and_audit_tabs`)*
- [ ] Populated state: success/convergence/escalation rates, per-role cost
      table, p50/p95 latency, daily trend, budget usage, prompt-version mix,
      human-vs-agent comparison, price-table stamp. *(automated)*

## Audit tab

- [ ] Job select lists recent jobs; verify renders chain entries, manifest
      root, and evidence manifest items with sha256 prefixes.
- [ ] Valid chain → `valid` badge; tampered transcript → `INVALID` badge.
      *(automated: `test_performance_and_audit_tabs`)*

## Accessibility

- [ ] All form inputs wrapped in `<label>` or carry `aria-label`.
- [ ] Focus-visible outlines on tabs, prompt names, inputs, buttons.
- [ ] SSE feed uses `aria-live="polite"` (assertive would spam on every
      token).
- [ ] Modal dismisses on Escape and backdrop click; focus returns sensibly.
- [ ] Colour is not the only signal — status chips carry text labels.
