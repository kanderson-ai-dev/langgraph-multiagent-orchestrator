# Contributing

## Setup

```bash
uv sync --all-extras
cp .env.example .env   # fill in keys as needed — the app degrades cleanly without them
uv run uvicorn app.main:app --reload
```

## Conventions

- Python ≥ 3.11, package manager `uv`. `requirements.txt` is generated (`uv export`),
  never hand-edited.
- `ruff check .` must pass (line-length 100, rules `E,F,I,UP,B,SIM`, ignore `B008`).
- `mypy --strict app/` must pass with 0 errors.
- Tests must be deterministic and fully offline — no network, no real credentials.
  Credential-dependent tests use the `requires_*` markers in `tests/conftest.py`.
- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- Never commit secrets. `.env`, `data/*.sqlite`, and internal planning docs are
  gitignored.
