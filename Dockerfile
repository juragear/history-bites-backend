# syntax=docker/dockerfile:1.7

# ---------- Build stage ----------
FROM python:3.12-slim-bookworm AS builder

# Install uv from upstream image (version pinned)
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /build

# Build the venv at its final runtime path so shebangs are correct from the start
# (Python venvs hardcode absolute paths into entry-point script shebangs;
# building at the runtime path avoids needing relocation.)
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Copy lockfile first for layer caching
COPY pyproject.toml uv.lock ./

# Install deps to /opt/venv (no dev, no project install — project copied in next layer)
RUN uv sync --frozen --no-dev --no-install-project

# Copy app code + migrations
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# ---------- Runtime stage ----------
FROM python:3.12-slim-bookworm AS runtime

# Non-root user
RUN groupadd --system app && useradd --system --gid app app

# Copy installed deps and app from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=app:app /build/app /app/app
COPY --from=builder --chown=app:app /build/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /build/migrations /app/migrations

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
WORKDIR /app

# Cloud Run injects PORT; default for local sanity
ENV PORT=8080

# Cloud Run expects the app to listen on $PORT
CMD exec uvicorn app.main:app --host 0.0.0.0 --port $PORT
