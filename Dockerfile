# syntax=docker/dockerfile:1

# ─────────────────────────────────────────────────────────────
# Stage 1 — builder: install dependencies into an isolated venv
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for psycopg2-binary / Pillow builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /venv && /venv/bin/pip install --upgrade pip && /venv/bin/pip install -r requirements.txt

# ─────────────────────────────────────────────────────────────
# Stage 2 — runtime: minimal image with dependencies + app code
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="Ultimate Group Manager" \
      org.opencontainers.image.description="Telegram group management bot + web dashboard (PostgreSQL-only)" \
      org.opencontainers.image.source="https://github.com/RahatCodes7/Group-manager" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    PORT=8000

WORKDIR /app

# Runtime lib for psycopg2 (libpq5 only — not the build headers)
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder --chown=app:app /venv /venv

# Application code: the whole project, minus anything listed in .dockerignore.
# New modules and folders are included automatically — no per-file list to maintain.
COPY --chown=app:app . ./

USER app

EXPOSE 8000

# Health endpoint — no curl in slim images; use the stdlib instead
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\",timeout=5)"]

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
