# =============================================================================
# LuxSwirl — observability monitoring platform
# Version + build info: coded, NOT derived from the directory path.
# Source of truth: the VERSION file at the repo root. Bump it on release.
# Fallbacks: git describe (if VERSION missing but inside a git repo), then dev.
#
# Two baked images (backend + agent) published to ghcr.io/luxardolabs.
# Compose never builds for prod — the Makefile builds + pushes; compose pulls.
# =============================================================================

VERSION   := $(shell cat VERSION 2>/dev/null || git -c safe.directory=$(CURDIR) describe --tags --always 2>/dev/null || echo "0.0.0-dev")
TIMESTAMP := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
COMMIT    := $(shell git -c safe.directory=$(CURDIR) rev-parse --short HEAD 2>/dev/null || echo 'local')

# Registry / images. Public images live at ghcr.io/luxardolabs/luxswirl-{backend,agent}
# (the org is part of *_NAME below). Override REGISTRY (env var or
# `make <target> REGISTRY=...`) to push to a different registry.
REGISTRY        ?= ghcr.io
BACKEND_NAME    := luxardolabs/luxswirl-backend
AGENT_NAME      := luxardolabs/luxswirl-agent
BACKEND_IMAGE   := $(REGISTRY)/$(BACKEND_NAME)
AGENT_IMAGE     := $(REGISTRY)/$(AGENT_NAME)

# Multi-arch registry pushes (amd64 + arm64). Local build / build-dev stay single-
# arch — you can't --load a multi-platform image. Override with e.g. PLATFORMS=linux/amd64.
PLATFORMS       ?= linux/amd64,linux/arm64
BUILDX_BUILDER  := luxswirl-builder

# Build args baked into both images (exposed as ENV + LABEL at runtime).
BUILD_ARGS := --build-arg BUILD_VERSION=$(VERSION) \
              --build-arg BUILD_TIMESTAMP=$(TIMESTAMP) \
              --build-arg BUILD_COMMIT=$(COMMIT)

# Cache busting: set NOCACHE=1 on any build target to force a clean rebuild.
NOCACHE ?=
NO_CACHE_FLAG := $(if $(NOCACHE),--no-cache,)

# Poetry runs in docker — hosts have no host poetry (it lives only in the
# Dockerfile builder stage). A throwaway python:3.14-slim (same base as the
# images) installs poetry into a /tmp venv with the component dir mounted, so a
# regenerated poetry.lock is written back to the host. Runs AS the repo owner so
# the lock isn't left root-owned on the 1000-owned (NFS) tree.
REPO_UID := $(shell stat -c %u . 2>/dev/null || echo 1000)
REPO_GID := $(shell stat -c %g . 2>/dev/null || echo 1000)
POETRY_SPEC := poetry$(if $(POETRY_VERSION),==$(POETRY_VERSION),)
# $(call poetry_cmd,<component-dir>,<poetry args>)
define poetry_cmd
docker run --rm -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD)/$(1):/work -w /work \
  python:3.14-slim sh -c 'python -m venv /tmp/v && /tmp/v/bin/pip install -q $(POETRY_SPEC) && /tmp/v/bin/poetry $(2)'
endef

# Compose invocations (base + environment overlay).
DC      := docker compose -f compose.yaml
DC_DEV  := docker compose -f compose.yaml -f compose.dev.yaml
DC_PROD := docker compose -f compose.yaml -f compose.prod.yaml
DC_TEST := docker compose -f compose.test.yaml
# Sandbox: own project name (isolates network/containers) + own env-file (isolates
# interpolation: POSTGRES_PASSWORD, LUXSWIRL_AUTH_KEY, secrets) + the sandbox overlay.
DC_SBX  := docker compose -p luxswirl-sbx --env-file .env.sandbox -f compose.yaml -f compose.sandbox.yaml

# ANSI colors for `make help`.
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
CYAN := \033[0;36m
NC := \033[0m
BOLD := \033[1m

.PHONY: help version \
        build build-backend build-agent build-dev push release \
        backend-image agent-image \
        dev dev-up dev-down dev-logs dev-shell dev-agent-shell dev-restart dev-rebuild \
        prod prod-up prod-down prod-logs \
        db-shell db-backup \
        status logs clean clean-all docker-clean \
        css css-watch \
        poetry-lock poetry-update poetry-install \
        compileall lint-build lint format mypy arch gitleaks check \
        test test-down

.DEFAULT_GOAL := help

##@ General

help: ## Show this grouped command help
	@printf "\n$(BOLD)$(CYAN)LuxSwirl — observability platform$(NC)\n"
	@printf "$(YELLOW)Version: $(VERSION)  |  Commit: $(COMMIT)$(NC)\n"
	@awk 'BEGIN {FS = ":.*?## "} \
		/^##@/ { printf "\n$(BOLD)$(BLUE)%s$(NC)\n", substr($$0, 5); next } \
		/^[a-zA-Z0-9_-]+:.*?## / { printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\n"

version: ## Show version / build info
	@echo "Version:    $(VERSION)"
	@echo "Commit:     $(COMMIT)"
	@echo "Timestamp:  $(TIMESTAMP)"
	@echo "Backend:    $(BACKEND_IMAGE):$(VERSION)"
	@echo "Agent:      $(AGENT_IMAGE):$(VERSION)"

# =============================================================================
# Docker — Build & Registry (one baked image per component; compose pulls)
# =============================================================================

##@ Docker — Build & Registry

backend-image: ## Build backend image as :$(VERSION) (no push)
	docker build $(NO_CACHE_FLAG) -f apps/backend/Dockerfile $(BUILD_ARGS) \
		-t $(BACKEND_IMAGE):$(VERSION) .

agent-image: ## Build agent image as :$(VERSION) (no push)
	docker build $(NO_CACHE_FLAG) -f apps/agent/Dockerfile $(BUILD_ARGS) \
		-t $(AGENT_IMAGE):$(VERSION) .

build: backend-image agent-image ## Build both images as :$(VERSION) (no push)

buildx-setup: ## Create/use the multi-arch buildx builder (docker-container driver + QEMU)
	@docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 || \
		docker buildx create --name $(BUILDX_BUILDER) --driver docker-container --bootstrap --use
	@docker buildx use $(BUILDX_BUILDER)

build-backend: buildx-setup ## Build + push backend :$(VERSION) AND :latest (multi-arch: $(PLATFORMS))
	docker buildx build $(NO_CACHE_FLAG) --platform $(PLATFORMS) -f apps/backend/Dockerfile $(BUILD_ARGS) \
		-t $(BACKEND_IMAGE):$(VERSION) -t $(BACKEND_IMAGE):latest --push .

build-agent: buildx-setup ## Build + push agent :$(VERSION) AND :latest (multi-arch: $(PLATFORMS))
	docker buildx build $(NO_CACHE_FLAG) --platform $(PLATFORMS) -f apps/agent/Dockerfile $(BUILD_ARGS) \
		-t $(AGENT_IMAGE):$(VERSION) -t $(AGENT_IMAGE):latest --push .

build-dev: ## Build both images locally tagged :dev (for the dev stack)
	docker build $(NO_CACHE_FLAG) -f apps/backend/Dockerfile $(BUILD_ARGS) -t $(BACKEND_IMAGE):dev .
	docker build $(NO_CACHE_FLAG) -f apps/agent/Dockerfile $(BUILD_ARGS) -t $(AGENT_IMAGE):dev .

push: build-backend build-agent ## Build + push both images (:$(VERSION) + :latest)

release: css push ## Promote a release: build CSS, build + push both images (:$(VERSION) + :latest)
	@printf "$(GREEN)Released $(VERSION) → $(REGISTRY)$(NC)\n"

# =============================================================================
# Dev / Prod stacks
# =============================================================================

##@ Dev stack (compose.yaml + compose.dev.yaml — baked images, no source mounts)

dev: dev-up ## Alias for dev-up
dev-up: ## Start the dev stack (does NOT build — run `make build-dev` first to pick up code changes)
	$(DC_DEV) up -d
	@echo "LuxSwirl $(VERSION) [dev] — server on :9000 (via nginx)"

dev-down: ## Stop the dev stack
	$(DC_DEV) down

dev-restart: ## Restart the dev stack
	$(DC_DEV) restart

dev-rebuild: ## Rebuild dev images from scratch and restart
	$(DC_DEV) build --no-cache
	$(DC_DEV) up -d

dev-logs: ## Follow dev logs
	$(DC_DEV) logs -f

dev-shell: ## Bash into the dev server container
	$(DC_DEV) exec luxswirl_server /bin/bash

dev-agent-shell: ## Bash into the dev agent container
	$(DC_DEV) exec luxswirl_agent /bin/bash

##@ Prod stack (compose.yaml + compose.prod.yaml — pulls baked images)

prod: prod-up ## Alias for prod-up
prod-up: ## Pull + start the production stack
	$(DC_PROD) pull
	$(DC_PROD) up -d --no-build
	@echo "LuxSwirl $(VERSION) [prod] running"

prod-down: ## Stop the production stack
	$(DC_PROD) down

prod-logs: ## Follow production logs
	$(DC_PROD) logs -f

##@ Sandbox stack (disposable, fully isolated — for testing a new version; leaves dev alone)

sandbox: sandbox-up ## Alias for sandbox-up
sandbox-up: build-dev ## Build :dev images from current source, then start the isolated sandbox
	@mkdir -p data/sandbox/server data/sandbox/agent
	$(DC_SBX) up -d
	@echo "LuxSwirl $(VERSION) [sandbox] — login at https://localhost:9001 (dev untouched, db on :5433)"

sandbox-down: ## Stop the sandbox stack (keeps its DB volume for next time)
	$(DC_SBX) down

sandbox-clean: ## Stop the sandbox AND wipe its DB volume + data dirs (fresh first-run next up)
	$(DC_SBX) down -v
	@rm -rf data/sandbox
	@echo "Sandbox wiped — next 'make sandbox-up' starts at the /setup wizard."

sandbox-restart: ## Restart the sandbox stack
	$(DC_SBX) restart

sandbox-logs: ## Follow sandbox logs
	$(DC_SBX) logs -f

sandbox-shell: ## Bash into the sandbox server container
	$(DC_SBX) exec luxswirl_server /bin/bash

sandbox-status: ## Show sandbox containers
	$(DC_SBX) ps

sandbox-db-shell: ## psql into the sandbox database
	$(DC_SBX) exec timescaledb psql -U luxswirl -d luxswirl

# =============================================================================
# Database
# =============================================================================

##@ Database

db-shell: ## psql into the running TimescaleDB
	$(DC_DEV) exec timescaledb psql -U luxswirl -d luxswirl

db-backup: ## pg_dump the database to backups/YYYY/MM/DD/luxswirl_<ts>.dump
	@d=$$(date -u +%Y/%m/%d) && ts=$$(date -u +%Y%m%d_%H%M%S) && mkdir -p backups/$$d && \
	  $(DC_DEV) exec -T timescaledb pg_dump -U luxswirl -Fc luxswirl > backups/$$d/luxswirl_$${ts}.dump && \
	  ls -lh backups/$$d/luxswirl_$${ts}.dump

# =============================================================================
# Frontend / CSS (Tailwind lives under apps/backend)
# =============================================================================

##@ Frontend / CSS

css: ## Build production CSS (minified) — in docker, no host node needed
	docker run --rm -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD)/apps/backend:/work -w /work \
	  node:20-slim sh -c 'npm ci --no-audit --no-fund && npm run build'

css-watch: ## Build CSS in watch mode — in docker
	docker run --rm -it -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD)/apps/backend:/work -w /work \
	  node:20-slim sh -c 'npm ci --no-audit --no-fund && npm run dev'

# =============================================================================
# Dependencies (poetry in docker — no host poetry required). Per component.
# =============================================================================

##@ Dependencies

poetry-lock: ## Regenerate poetry.lock for BOTH components (no install)
	$(call poetry_cmd,apps/backend,lock)
	$(call poetry_cmd,apps/agent,lock)

poetry-update: ## Update deps to latest allowed + rewrite BOTH locks
	$(call poetry_cmd,apps/backend,update --lock)
	$(call poetry_cmd,apps/agent,update --lock)

poetry-install: ## Verify BOTH components resolve + install from lock (throwaway)
	$(call poetry_cmd,apps/backend,install --no-root --only main)
	$(call poetry_cmd,apps/agent,install --no-root --only main)

# =============================================================================
# Quality (lint · types · architecture · tests · secrets). Per component.
# Linters live in throwaway Dockerfile.lint images (the runtime images install
# --only main), built fresh from current source — never lints stale code.
# =============================================================================

##@ Quality

BACKEND_LINT := luxswirl-backend-lint
AGENT_LINT   := luxswirl-agent-lint
TEST_IMAGE   := luxswirl:test

compileall: ## Byte-compile all Python (fast syntax check) — in docker
	docker run --rm -u $(REPO_UID):$(REPO_GID) -v $(PWD):/repo -w /repo \
	  python:3.14-slim python -m compileall -q apps

lint-build: ## Build BOTH lint images (ruff + mypy) from current source
	DOCKER_BUILDKIT=1 docker build $(NO_CACHE_FLAG) -f apps/backend/Dockerfile.lint -t $(BACKEND_LINT) .
	DOCKER_BUILDKIT=1 docker build $(NO_CACHE_FLAG) -f apps/agent/Dockerfile.lint -t $(AGENT_LINT) .

lint: lint-build ## ruff check + format-check + mypy for BOTH components (boutique-style)
	docker run --rm $(BACKEND_LINT) ruff check app tests
	docker run --rm $(BACKEND_LINT) ruff format --check app
	docker run --rm $(BACKEND_LINT) mypy app
	docker run --rm $(AGENT_LINT) ruff check app tests
	docker run --rm $(AGENT_LINT) ruff format --check app
	docker run --rm $(AGENT_LINT) mypy app

format: lint-build ## ruff auto-fix + format for BOTH components (writes to host)
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff -u $(REPO_UID):$(REPO_GID) -v $(PWD)/apps/backend/app:/app/app -v $(PWD)/apps/backend/tests:/app/tests $(BACKEND_LINT) sh -c 'ruff check --fix app tests && ruff format app tests'
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff -u $(REPO_UID):$(REPO_GID) -v $(PWD)/apps/agent/app:/app/app -v $(PWD)/apps/agent/tests:/app/tests $(AGENT_LINT) sh -c 'ruff check --fix app tests && ruff format app tests'

mypy: lint-build ## mypy type-check BOTH components
	docker run --rm $(BACKEND_LINT) mypy app
	docker run --rm $(AGENT_LINT) mypy app

arch: lint-build ## Architecture + static lint guards (grep + import-linter) — ALWAYS baked fresh from on-disk source (CI gate; no :dev dependency)
	docker run --rm $(BACKEND_LINT) sh -c 'cd /app && pytest tests/test_architecture.py tests/test_no_redundant_db_commit.py tests/test_no_raw_js_network.py -q --no-cov'

gitleaks: ## Scan the tree for committed secrets (run before the first git commit)
	docker run --rm -v $(PWD):/repo -w /repo ghcr.io/gitleaks/gitleaks:latest \
	  dir /repo/apps -c /repo/.gitleaks.toml --no-banner --redact -v

check: lint test ## Full pre-commit suite — lint + types + tests, all in docker (boutique-style)

docker-clean: ## Remove locally built image tags (backend/agent :dev/:$(VERSION) + lint)
	-docker rmi $(BACKEND_IMAGE):dev $(AGENT_IMAGE):dev \
		$(BACKEND_IMAGE):$(VERSION) $(AGENT_IMAGE):$(VERSION) \
		$(BACKEND_LINT) $(AGENT_LINT) 2>/dev/null || true

# =============================================================================
# Tests (isolated test DB on tmpfs — mirrors luxwx)
# =============================================================================

##@ Tests

test-build: ## Build the test image from current source (self-contained — no :dev dependency)
	DOCKER_BUILDKIT=1 docker build $(NO_CACHE_FLAG) -f Dockerfile.test -t $(TEST_IMAGE) .

test: test-build ## Run the backend DB-integration suite against an isolated test DB (TEST=path/to/test for one file). Image is ALWAYS baked fresh from on-disk source (no :dev dependency). Static/arch guards run via `make arch`.
	$(DC_TEST) up -d --wait timescaledb-test
	$(DC_TEST) run --rm tests pytest $(TEST) --ignore=tests/test_architecture.py --ignore=tests/test_no_redundant_db_commit.py --ignore=tests/test_no_raw_js_network.py ; status=$$? ; $(DC_TEST) down -v ; exit $$status

test-down: ## Tear down the test stack
	$(DC_TEST) down -v

migration-check: test-build ## Verify alembic migrations match the models — `alembic check` on a fresh DB must be clean (LUXSWIRL-178)
	$(DC_TEST) up -d --wait timescaledb-test
	$(DC_TEST) run --rm tests sh -c 'alembic upgrade head && alembic check' ; status=$$? ; $(DC_TEST) down -v ; exit $$status

# =============================================================================
# Utilities
# =============================================================================

##@ Utilities

status: ## Show container status (dev stack)
	$(DC_DEV) ps

logs: ## Follow all logs (dev stack)
	$(DC_DEV) logs -f

clean: ## Remove stopped containers + orphans (dev stack)
	$(DC_DEV) down --remove-orphans

clean-all: ## Remove containers + volumes (DESTRUCTIVE — drops the DB)
	$(DC_DEV) down -v --remove-orphans
