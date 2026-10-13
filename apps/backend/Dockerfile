# LuxSwirl backend (FastAPI server + HTMX portal).
# Build context = repo root: `docker build -f apps/backend/Dockerfile .`
# No Playwright/Chromium here — synthetic checks run in the agent image only.
# The app directory within the repo-root build context, for the css stage below.
ARG APP_DIR=apps/backend

# luxarch:css-stage asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit css-stage`.
# ---- css: compile the stylesheet; only its output reaches the app image (luxarch --doc FLEET-BUILD-DEPLOY-STANDARD) ----
# Paste above your app stage, then copy ONLY the output into it:
#   COPY --from=css /build/app/static/css/app.css /app/app/static/css/app.css
# Node, node_modules and the Tailwind toolchain never reach the runtime image.
# APP_DIR is the app's directory WITHIN the build context; unset, it is the context itself. When the
# image builds from a wider context than the app (`-f apps/backend/Dockerfile` from the repo root, to
# vendor a shared package), bind it ONCE above the Dockerfile's first FROM, outside this block:
#   ARG APP_DIR=apps/backend
# The COPY --from line above is unchanged: the stage lays the app directory out at /build either way.
FROM node:24-slim AS css
ARG APP_DIR
WORKDIR /build
COPY ${APP_DIR:-.}/package.json ${APP_DIR:-.}/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ${APP_DIR:-.}/ .
RUN npm run build:css
# ---- end css stage ----

FROM python:3.14-slim AS production

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        iputils-ping \
        iproute2 && \
    rm -rf /var/lib/apt/lists/*

ENV POETRY_VERSION=2.5.1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

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
# The stylesheet the css stage compiled from this same source (gitignored on the host).
COPY --from=css --chown=luxswirl:luxswirl /build/app/static/css/app.css /app/app/static/css/app.css
COPY --chown=luxswirl:luxswirl apps/backend/alembic /app/alembic
COPY --chown=luxswirl:luxswirl apps/backend/alembic.ini /app/alembic.ini
COPY --chown=luxswirl:luxswirl apps/shared /app/shared
ENV PYTHONPATH=/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]

# ---- test: the production image plus the lock's dev group ----
# The suite runs on the same app layers production ships (FLEET-BUILD-DEPLOY-STANDARD;
# `make test-build` builds this stage as the image block's TEST_IMAGE). Production keeps
# poetry, so the dev group installs on top of it. Root, because poetry writes the
# system site-packages (POETRY_VIRTUALENVS_CREATE=false).
FROM production AS test
USER root
RUN poetry install --only dev --no-root && rm -rf $POETRY_CACHE_DIR
# The emitted migration-chain verifier (`make db-verify`) and the suite itself.
COPY apps/backend/scripts /app/scripts
COPY apps/backend/tests /app/tests
# /app resolves `app.*` and `shared.*`; /app/tests resolves `fixtures.*`. The canonical
# pytest config carries no `pythonpath`, so the path is the image's job.
ENV PYTHONPATH=/app:/app/tests
# Never a bare `pytest`: the canonical config is emitted per run by `make test` and
# mounted here; without it pytest exits rather than running unconfigured.
CMD ["sh", "-c", "exec pytest -c /app/.luxlint.pytest.ini"]
