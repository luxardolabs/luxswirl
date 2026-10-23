# LuxSwirl agent (health-check runner).
# Build context = repo root: `docker build -f apps/agent/Dockerfile .`
# Carries Playwright/Chromium (synthetic checks) + ping/iproute (network checks).
# ---- deps: the locked dependencies, built without shipping the tools that built them ----
# Poetry lives in its own venv here and never reaches the runtime image; the app's
# dependencies go into /opt/venv, created without pip, which is all production copies.
# Shipping pip/poetry put their vendored copies of urllib3, msgpack and setuptools in the
# image, and the release scan refuses those (FLEET-BUILD-DEPLOY-STANDARD, image leg).
FROM python:3.14-slim AS deps
ENV POETRY_VERSION=2.5.1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR='/var/cache/pypoetry'
RUN python -m venv /opt/poetry && \
    /opt/poetry/bin/pip install --no-cache-dir "poetry==${POETRY_VERSION}" && \
    python -m venv --without-pip /opt/venv
WORKDIR /app
COPY apps/agent/pyproject.toml apps/agent/poetry.lock* /app/
RUN VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH \
    /opt/poetry/bin/poetry install --only main --no-root && rm -rf $POETRY_CACHE_DIR

FROM python:3.14-slim AS production

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
    apt-get upgrade -y && \
    rm -rf /var/lib/apt/lists/*

# The base image's pip is a build tool the app never runs, and it vendors packages
# the scan refuses; the app's dependencies come from the deps stage's venv.
RUN python -m pip uninstall -y pip
COPY --from=deps /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

RUN useradd -m -u 1000 -s /bin/bash luxswirl

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

# app package -> /app/app. The shared SSOT (apps/shared) is baked over the app's
# 0-byte placeholder package app/shared, so it is imported as `app.shared.*`.
COPY --chown=luxswirl:luxswirl apps/agent/app /app/app
COPY --chown=luxswirl:luxswirl apps/shared /app/app/shared
ENV PYTHONPATH=/app

# Liveness = the agent's own work, not a live process: app/health/check.py asserts the
# heartbeat liveness stamp is fresh, so a wedged agent reads unhealthy (see that module).
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "app.health.check"]

CMD ["python", "-m", "app.main"]

# ---- test: the production image plus the lock's dev group ----
# The suite runs on the same app layers production ships; `make test` builds this stage
# for the agent suite (TEST_SUITES). Production carries no installer, so poetry is put
# back from the deps stage to add the dev group to the same /opt/venv. Root, because
# that venv belongs to root.
FROM production AS test
USER root
COPY --from=deps /opt/poetry /opt/poetry
COPY apps/agent/pyproject.toml apps/agent/poetry.lock* /app/
RUN VIRTUAL_ENV=/opt/venv POETRY_VIRTUALENVS_CREATE=false \
    /opt/poetry/bin/poetry install --only dev --no-root --no-cache
COPY apps/agent/tests /app/tests
# Never a bare `pytest`: the canonical config is emitted per run by `make test` and
# mounted at /cfg/pytest.ini (the test block's mount).
CMD ["sh", "-c", "exec pytest -c /cfg/pytest.ini"]
