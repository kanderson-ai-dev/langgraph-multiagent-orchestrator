# syntax=docker/dockerfile:1

# Base image pinned by digest (python:3.13-slim) — Dependabot's docker
# ecosystem bumps the pin on upstream rebuilds.
ARG PY_SLIM=python:3.13-slim@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f

# ── builder: resolve locked runtime deps, nothing ships from here ─────────────
FROM ${PY_SLIM} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# requirements.txt is an all-groups export (includes dev tools), so the image
# resolves runtime-only deps straight from the lockfile instead.
# --no-compile skips the .pyc pass; the find-pass strips any bytecode a wheel
# shipped anyway plus in-package test suites — none are needed at runtime.
COPY pyproject.toml uv.lock ./
RUN pip install uv \
    && uv export --frozen --format requirements-txt --no-dev --no-hashes \
       -o /build/requirements.txt \
    && pip install --prefix=/install --no-compile -r /build/requirements.txt \
    && find /install -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find /install -type d \( -name tests -o -name testing \) \
       -prune -exec rm -rf {} +

# ── runtime: app + deps only — no uv, no build tools, non-root ────────────────
FROM ${PY_SLIM} AS runtime

LABEL org.opencontainers.image.authors="Kaarsten Anderson <contact@kaarstenanderson.com>" \
      maintainer="Kaarsten Anderson <contact@kaarstenanderson.com>"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime deps land in the interpreter's standard prefix — no PATH hacks.
COPY --from=builder /install /usr/local

COPY app/ app/
COPY frontend/ frontend/
COPY prompts/ prompts/
COPY evaluation/ evaluation/

# Non-root runtime user; data/ holds the SQLite job/checkpoint stores and is
# the volume mount point for persistence. pip/setuptools ship in the slim base
# but are useless at runtime — removing them shrinks the image and the
# post-compromise toolset (no interpreter-assisted installs).
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
       /usr/local/lib/python3.13/site-packages/setuptools* \
       /usr/local/lib/python3.13/site-packages/_distutils_hack \
       /usr/local/bin/pip* \
    && useradd --system --uid 10001 --home /app appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
