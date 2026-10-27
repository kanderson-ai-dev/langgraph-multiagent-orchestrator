# Why Langfuse (and when LangSmith is used instead)

This project supports two tracing backends behind one flag:

| `TRACING_PROVIDER` | Backend | When it applies |
|---|---|---|
| `langsmith` | LangSmith | Default choice — `LANGCHAIN_API_KEY` + `LANGCHAIN_TRACING_V2=true`. |
| `langfuse` | Langfuse (cloud or self-hosted) | Fallback when LangSmith is unavailable — e.g. a revoked key or an exhausted monthly trace quota. |
| `none` | — | Default in `.env.example`: zero overhead, fully offline. |

## What triggered the fallback here

During evaluation work, the LangSmith account authenticated fine but every
trace-ingest call returned `429 — monthly unique traces usage limit
exceeded`. Per the project's own availability rule, a quota `429` counts as
unavailable, so the conditional Langfuse path was built. It stays behind the
flag permanently: if LangSmith quota returns, flipping
`TRACING_PROVIDER=langsmith` is the only change needed — no code edits.

## Why Langfuse specifically

- **Equivalent data model**: traces → observations (spans/generations),
  datasets → experiments → scores. The LangSmith experiment screenshots in
  this README have a direct Langfuse counterpart.
- **OpenTelemetry-native SDK (v4)**: the LangChain `CallbackHandler` drops
  into the same `config={"callbacks": [...]}` seam LangSmith uses — the
  graph code never learns which backend is listening.
- **Built-in `run_experiment`**: the same EDD dataset of report briefs
  uploads as a Langfuse dataset; per-case scores
  (`citation_support`, `rubric_score`, `judge_score`, `converged`,
  `blocked_correctly`) land as experiment scores — the local
  `scorecard.json` remains the gate, Langfuse is only the viewer.
- **Reads go through the experiments API**: score reads use
  `client.api.experiments.list` (`fetch_experiment_scores` in
  `evaluation/langfuse_eval.py`), not the legacy `dataset_runs`
  endpoint — the datasets→experiments rename made the former the
  supported read path. Reads are optional and degrade to `None` when
  the SDK or run is absent.
- **Optional self-host**: `docker-compose.langfuse.yml` runs the full stack
  locally with no external quota at all.

## Privacy notes

- Payload masking (`Langfuse(mask=...)`) redacts `*_key`/`*_token`/
  `authorization`/`*_secret` fields and **system-prompt message content**
  before export — prompt templates are IP; provenance travels as
  `prompt_version` + `prompt_sha256` metadata and `prompt:<name>@v<N>` tags.
- User input and model output do leave the process for the region in
  `LANGFUSE_BASE_URL` when Langfuse is active — pick the region (or the
  self-host stack) accordingly.
- Free-tier limits change; check <https://langfuse.com/pricing> before
  relying on cloud quotas for CI.
