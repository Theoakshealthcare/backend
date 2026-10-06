# syntax=docker/dockerfile:1.4

ARG PYTHON_VERSION=3.13-slim-bookworm

# ---- Build stage: resolve and install dependencies with Poetry ----
FROM python:${PYTHON_VERSION} AS builder

ARG POETRY_VERSION=2.2.1
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

WORKDIR /app

# Copy dependency manifests first for better layer caching
COPY pyproject.toml poetry.lock ./

RUN poetry install --only main --no-root && rm -rf "$POETRY_CACHE_DIR"

# ---- Runtime stage ----
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Runtime system dependencies (netcat is used by the boot script's DB wait loop)
RUN apt-get update && apt-get install -y --no-install-recommends \
    netcat-traditional \
    libpq5 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# Copy application code
RUN mkdir -p /app/private_uploads /app/uploads

COPY ./app /app/app

# Copy alembic configuration
COPY ./alembic.ini /app/alembic.ini

# Operational scripts (dev DB bootstrap, health-data re-encryption)
COPY ./scripts /app/scripts

# Copy run script
COPY ./boot/docker-run.sh /opt/run.sh
RUN chmod +x /opt/run.sh

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app
# Private upload volume mount point (owned by the app user so a fresh named volume is writable)

USER app

EXPOSE 8000

CMD ["/opt/run.sh"]
