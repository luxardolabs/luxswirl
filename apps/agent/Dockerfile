# LuxSwirl agent (health-check runner).
# Build context = repo root: `docker build -f apps/agent/Dockerfile .`
# Carries Playwright/Chromium (synthetic checks) + ping/iproute (network checks).
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        iputils-ping \
        iproute2 \
        curl \
        # Playwright/Chromium system dependencies
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libatspi2.0-0 \
        libwayland-client0 && \
    rm -rf /var/lib/apt/lists/*

ENV POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN curl -sSL https://install.python-poetry.org | python3 - && \
    ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

RUN useradd -m -u 1000 -s /bin/bash luxswirl

COPY apps/agent/pyproject.toml apps/agent/poetry.lock* /app/
RUN poetry install --only main --no-root && rm -rf $POETRY_CACHE_DIR

RUN chown -R luxswirl:luxswirl /app
USER luxswirl
RUN mkdir -p /app/logs /app/reports /app/data

# Chromium binary (cached unless poetry.lock changes the Playwright version).
RUN playwright install chromium

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
      org.opencontainers.image.title="LuxSwirl Agent" \
      org.opencontainers.image.description="LuxSwirl monitoring agent — runs health checks (ping, HTTP, TCP, JSON, DNS, MySQL, PostgreSQL, synthetic) and reports results to the server." \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}"

# app package -> /app/app ; shared -> /app/shared.
# PYTHONPATH=/app resolves both `import app.*` and `import shared.*`.
COPY --chown=luxswirl:luxswirl apps/agent/app /app/app
COPY --chown=luxswirl:luxswirl apps/shared /app/shared
ENV PYTHONPATH=/app

CMD ["python", "-m", "app.agent_main"]
