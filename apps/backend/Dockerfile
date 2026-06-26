# LuxSwirl backend (FastAPI server + HTMX portal).
# Build context = repo root: `docker build -f apps/backend/Dockerfile .`
# No Playwright/Chromium here — synthetic checks run in the agent image only.
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        iputils-ping \
        iproute2 && \
    rm -rf /var/lib/apt/lists/*

ENV POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN curl -sSL https://install.python-poetry.org | python3 - && \
    ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

RUN useradd -m -u 1000 -s /bin/bash luxswirl

# Dependency files first for layer caching (component-local pyproject/lock).
COPY apps/backend/pyproject.toml apps/backend/poetry.lock* /app/
RUN poetry install --only main --no-root && rm -rf $POETRY_CACHE_DIR

EXPOSE 9000

RUN chown -R luxswirl:luxswirl /app
USER luxswirl
RUN mkdir -p /app/logs /app/reports /app/data

# ---- Layers below invalidate on every build ----
ARG BUILD_VERSION=dev
ARG BUILD_TIMESTAMP=unknown
ARG BUILD_COMMIT=local
ENV APP_VERSION=${BUILD_VERSION} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP} \
    BUILD_COMMIT=${BUILD_COMMIT}
LABEL version="${BUILD_VERSION}" build_timestamp="${BUILD_TIMESTAMP}" commit="${BUILD_COMMIT}"
LABEL org.opencontainers.image.source="https://github.com/luxardolabs/luxswirl" \
      org.opencontainers.image.url="https://github.com/luxardolabs/luxswirl" \
      org.opencontainers.image.title="LuxSwirl Backend" \
      org.opencontainers.image.description="LuxSwirl monitoring server — REST API, web dashboard, and Prometheus metrics (FastAPI + TimescaleDB)." \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}"

# app package -> /app/app ; shared -> /app/shared ; alembic at component root.
# PYTHONPATH=/app resolves both `import app.*` and `import shared.*`.
COPY --chown=luxswirl:luxswirl apps/backend/app /app/app
COPY --chown=luxswirl:luxswirl apps/backend/alembic /app/alembic
COPY --chown=luxswirl:luxswirl apps/backend/alembic.ini /app/alembic.ini
COPY --chown=luxswirl:luxswirl apps/shared /app/shared
ENV PYTHONPATH=/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
