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
- `pytest` leaves `git status` clean: generated QA artifacts (screenshots,
  `RUNS.md`) go to a temp dir. Refresh the committed copies deliberately with
  `uv run python scripts/refresh_qa_artifacts.py` and review the diff before
  committing.
- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- Never commit secrets. `.env`, `data/*.sqlite`, and internal planning docs are
  gitignored.

## Verify before committing

```bash
uv run ruff check .                                   # lint
uv run pytest -q                                      # full suite, offline
uv run pytest --cov=app --cov-report=term-missing     # coverage >= 85%
uv run mypy --strict app/                             # 0 errors
uv run python -m evaluation.run_eval                  # offline EDD gate
gitleaks detect                                       # no secrets in history
uv run pip-audit --strict                             # after dependency changes
uv lock --check                                       # after touching uv.lock
docker build -t orchestrator:check .                  # after Dockerfile/compose changes
```

On Windows, bare `python`/`pytest` may be an older Python — always go through
`uv run`.
