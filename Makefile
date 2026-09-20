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

# Architecture guard (luxarch) — internal tooling on a private registry (not on ghcr,
# not part of this repo's public surface). Supply the registry host out-of-tree via
# Makefile.local (gitignored): copy Makefile.local.example -> Makefile.local and set it.
# Contributors don't need it — lint/test and the import-linter contracts run without it.
LUXARCH_REGISTRY ?=
LUXARCH_VERSION := 0.193.0
LUXARCH_IMAGE    ?= $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$(LUXARCH_VERSION)
# Dependency-CVE guard (luxaudit) — same private registry as luxarch, mount-only.
# Scans the pinned poetry.locks against the live advisory feed (see .luxaudit.toml).
LUXAUDIT_VERSION := 0.9.0
LUXAUDIT_IMAGE   ?= $(LUXARCH_REGISTRY)/luxardolabs/luxaudit:$(LUXAUDIT_VERSION)
# Lint/type/test-standard guard (luxlint) — same private registry. Owns the fleet
# ruff/mypy config byte-identical (see .luxlint.toml); ruff runs mount-only, mypy
# runs as an in-repo tail (needs a current mypy for PEP 758 excepts — the baked
# lint image freezes an older one, so the tail installs a fresh mypy).
LUXLINT_VERSION := 0.55.0
LUXLINT_IMAGE    ?= $(LUXARCH_REGISTRY)/luxardolabs/luxlint:$(LUXLINT_VERSION)
-include Makefile.local

# Multi-arch registry pushes (amd64 + arm64). Local build / build-dev stay single-
# arch — you can't --load a multi-platform image. Override with e.g. PLATFORMS=linux/amd64.
PLATFORMS       ?= linux/amd64,linux/arm64
# ONE shared fleet builder — never a per-project `<repo>-builder`. Ten per-project builders = ten
# un-shared caches (no base-layer/pip/npm dedup) = tens of GB idle; the shared builder dedups and
# cross-project cache-hits speed builds. Enforced by repo.shared_buildx_builder; see FLEET-BUILD-DEPLOY-STANDARD.
BUILDX_BUILDER  ?= luxardo-builder

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

# $(call mypy_leg,<component>) — the luxlint mypy tail for ONE app leg. Runs a FRESH
# mypy (+pydantic for the plugin) on a lean slim image, NOT the baked lint image: the
# latter freezes mypy 2.1.0 which crashes on this repo's PEP 758 `except A, B:` — the
# tail is versioned by luxlint, decoupled from the app's dep freeze. MYPYPATH carries
# the leg's own root (apps/<c> → `app`) plus apps/ (→ vendored `shared`); the canonical
# config (ignore_missing_imports) treats the rest as Any. Config emitted to .luxlint.mypy.ini.
define mypy_leg
docker run --rm -v $(PWD):/w -w /w python:3.14-slim sh -c 'pip install -q -U mypy pydantic && MYPYPATH=/w/apps/$(1):/w/apps mypy --config-file /w/.luxlint.mypy.ini apps/$(1)/app'
endef

# Compose invocations. ONE compose.yml; the environment is the --env-file, not
# an overlay (repo.compose_conventions / FLEET-BUILD-DEPLOY-STANDARD).
DC      := docker compose -f compose.yml
DC_DEV  := docker compose -f compose.yml --env-file .env.dev
DC_PROD := docker compose -f compose.yml --env-file .env.prod
# The test topology is a PROFILE in the one compose.yml, not a second file.
# .env.test is committed (it holds no secrets) because compose interpolates
# every service in the file, active profile or not.
DC_TEST := docker compose -p luxswirl-test --env-file .env.test --profile test
# Sandbox: own project name (isolates network/containers) + own env-file (isolates
# interpolation: POSTGRES_PASSWORD, LUXSWIRL_AUTH_KEY, secrets) + the sandbox overlay.
# The sandbox is an ENVIRONMENT, not an overlay: .env.sandbox supplies the
# container suffix, data dir, volume prefix, ports and image tags.
DC_SBX  := docker compose -p luxswirl-sbx --env-file .env.sandbox

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
        ps logs clean clean-all docker-clean \
        css css-watch \
        poetry-lock poetry-update poetry-install \
        compileall lint-build lint format mypy arch audit gitleaks onboard-check check \
        guard-version-check guard-upgrade honest status db-verify \
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

buildx-setup: ## Ensure the ONE shared multi-arch buildx builder exists, and REFUSE strays
	@mkdir -p $(HOME)/.docker
	@[ -f $(HOME)/.docker/buildkitd.toml ] || printf '[worker.oci]\n  gc = true\n  [[worker.oci.gcpolicy]]\n    keepBytes = "20GB"\n    all = true\n' > $(HOME)/.docker/buildkitd.toml
	@docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 || docker buildx create --name $(BUILDX_BUILDER) --driver docker-container --buildkitd-config $(HOME)/.docker/buildkitd.toml --bootstrap --use
	@docker buildx use $(BUILDX_BUILDER)
	@strays=$$(docker buildx ls 2>/dev/null | awk '$$2=="docker-container"{print $$1}' \
	  | grep -v '^\\_' | sed 's/\*$$//' | grep -vxF "$(BUILDX_BUILDER)" | tr '\n' ' '); \
	if [ -n "$$strays" ] && [ -z "$(ALLOW_STRAY_BUILDERS)" ]; then \
	  echo "REFUSING: stray per-project buildx builders are running: $$strays"; \
	  echo "Each keeps its OWN cache with no GC policy. Four orphans reached 100GB"; \
	  echo "on the fleet build host and killed a production Postgres."; \
	  echo "Remove them:  docker buildx rm $$strays"; \
	  exit 1; \
	fi


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

github-release: ## Publish the GitHub Release for $(VERSION) from its release notes
	@notes=apps/backend/app/release_notes/$(VERSION).md; \
	if [ ! -f $$notes ]; then \
		echo "release notes missing: $$notes — write them before releasing (see luxarch --doc FLEET-RELEASE-PROCESS)"; exit 1; \
	fi; \
	if ! command -v gh >/dev/null 2>&1; then \
		echo "gh not installed — the GitHub Release carries this version's notes and is not optional"; exit 1; \
	fi; \
	if gh release view v$(VERSION) >/dev/null 2>&1; then \
		echo ">> GitHub Release v$(VERSION) already exists — leaving it alone"; \
	else \
		gh release create v$(VERSION) --title "$(VERSION)" --notes-file $$notes; \
	fi

release: css push github-release ## Promote a release: CSS, push both images, publish the GitHub Release
	@printf "$(GREEN)Released $(VERSION) → $(REGISTRY)$(NC)\n"

# =============================================================================
# Dev / Prod stacks
# =============================================================================

##@ Dev stack (compose.yml + .env.dev — baked images, no source mounts)

dev: dev-up ## Alias for dev-up
dev-up: ## Start the dev stack (does NOT build — run `make build-dev` first to pick up code changes)
	$(DC_DEV) up -d
	@echo "LuxSwirl $(VERSION) [dev] — server on :9000 (via nginx)"

dev-down: ## Stop the dev stack
	$(DC_DEV) down

dev-restart: ## Restart the dev stack
	$(DC_DEV) restart

dev-rebuild: ## Rebuild dev images from scratch and restart
	$(MAKE) build-dev NO_CACHE_FLAG=--no-cache
	$(DC_DEV) up -d

dev-logs: ## Follow dev logs
	$(DC_DEV) logs -f

dev-shell: ## Bash into the dev server container
	$(DC_DEV) exec luxswirl_server /bin/bash

dev-agent-shell: ## Bash into the dev agent container
	$(DC_DEV) exec luxswirl_agent /bin/bash

##@ Prod stack (compose.yml + .env.prod — pulls baked images)

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

lint: ## luxlint ruff (mount-only, canonical set) + the mypy tail per leg — ONE recipe
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> luxlint skipped: LUXARCH_REGISTRY unset (internal tooling — see Makefile.local.example)"; exit 0; \
	fi
	@set +e; rc=0; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE); ruff=$$?; [ $$ruff -ne 0 ] && rc=1; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config mypy > $(PWD)/.luxlint.mypy.ini; \
	$(call mypy_leg,backend); be=$$?; [ $$be -ne 0 ] && rc=1; \
	$(call mypy_leg,agent); ae=$$?; [ $$ae -ne 0 ] && rc=1; \
	rm -f $(PWD)/.luxlint.mypy.ini; \
	if [ $$rc -ne 0 ]; then echo "lint FAILED (luxlint-ruff=$$ruff mypy-backend=$$be mypy-agent=$$ae)"; exit 1; fi

format: ## THE canonical fixer — `luxlint --format` (ruff fix+format AND markdown), in place. PATH=<substr> to scope.
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> format skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --format $(if $(PATH_SCOPE),--path $(PATH_SCOPE),)

mypy: ## mypy type-check BOTH components (canonical luxlint config, fresh mypy)
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> mypy skipped: LUXARCH_REGISTRY unset (needs the luxlint mypy config — see Makefile.local.example)"; exit 0; \
	fi
	@set +e; rc=0; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --emit-config mypy > $(PWD)/.luxlint.mypy.ini; \
	$(call mypy_leg,backend); be=$$?; [ $$be -ne 0 ] && rc=1; \
	$(call mypy_leg,agent); ae=$$?; [ $$ae -ne 0 ] && rc=1; \
	rm -f $(PWD)/.luxlint.mypy.ini; \
	if [ $$rc -ne 0 ]; then echo "mypy FAILED (backend=$$be agent=$$ae)"; exit 1; fi

guard-version-check: ## FATAL: fail if any guard pin is behind the published latest
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> guard-version-check skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	@rc=0; for g in luxarch luxlint luxaudit; do \
	  pin=$$(case $$g in luxarch) echo $(LUXARCH_VERSION);; luxlint) echo $(LUXLINT_VERSION);; luxaudit) echo $(LUXAUDIT_VERSION);; esac); \
	  docker pull -q $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest >/dev/null 2>&1 || true; \
	  latest=$$(docker run --rm $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest --version 2>/dev/null | awk '{print $$2}'); \
	  if [ -n "$$latest" ] && [ "$$latest" != "$$pin" ]; then \
	    printf "\342\234\227 %s pinned %s, latest %s \342\200\224 behind. Preview: --new-rules --since %s; then make guard-upgrade\n" "$$g" "$$pin" "$$latest" "$$pin"; rc=1; \
	  fi; done; exit $$rc

guard-upgrade:  ## Bump every guard pin to the published latest (prints what newly bites)
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> guard-upgrade skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	@for g in luxarch luxlint luxaudit; do \
	  docker pull -q $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest >/dev/null 2>&1 || true; \
	  latest=$$(docker run --rm $(LUXARCH_REGISTRY)/luxardolabs/$$g:latest --version 2>/dev/null | awk '{print $$2}'); \
	  var=$$(echo $$g | tr a-z A-Z)_VERSION; \
	  old=$$(sed -n -E "s/^$$var[[:space:]]*:=[[:space:]]*//p" Makefile); \
	  if [ -z "$$old" ]; then echo "!! no $$var pin found in Makefile — NOT bumped"; continue; fi; \
	  if [ -z "$$latest" ]; then echo "!! could not read $$g:latest — $$var left at $$old"; continue; fi; \
	  checked=1; \
	  sed -i -E "s|^($$var[[:space:]]*:=[[:space:]]*).*|\\1$$latest|" Makefile; \
	  new=$$(sed -n -E "s/^$$var[[:space:]]*:=[[:space:]]*//p" Makefile); \
	  if [ "$$new" != "$$latest" ]; then echo "!! $$var did NOT change (still $$new)"; exit 1; fi; \
	  if [ "$$old" != "$$latest" ]; then echo "$$var $$old -> $$latest"; bumped=1; fi; \
	  [ "$$g" = luxarch ] && [ "$$old" != "$$latest" ] && docker run --rm -v $(PWD):/repo $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$$latest --new-rules --since $$old || true; \
	done; \
	if [ -n "$$bumped" ]; then echo "pins bumped — re-run make check"; \
	elif [ -n "$$checked" ]; then echo "all pins already at latest"; \
	else echo "!! could not reach the registry — NO pin was checked; currency NOT established"; exit 1; fi

honest: ## HONESTY gate — a green `make check` must mean nothing was silently unchecked
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> honest skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	@set +e; rc=0; \
	docker run --rm -v $(PWD):/repo $(LUXARCH_IMAGE) --assert-scans; a=$$?; [ $$a -ne 0 ] && rc=1; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --preflight;     l=$$?; [ $$l -ne 0 ] && rc=1; \
	if [ $$rc -ne 0 ]; then echo "honest FAILED (assert-scans=$$a preflight=$$l)"; exit 1; fi

arch: lint-build ## Architecture conformance via luxarch (private tooling) + import-linter contracts (in-repo)
	@if [ -n "$(LUXARCH_REGISTRY)" ]; then \
		docker run --rm -v $(PWD):/repo $(LUXARCH_IMAGE); \
	else \
		echo ">> luxarch skipped: LUXARCH_REGISTRY unset (internal tooling — see Makefile.local.example)"; \
	fi
	docker run --rm $(BACKEND_LINT) sh -c 'cd /app && python -c "import sys; from importlinter.cli import lint_imports_command; sys.exit(lint_imports_command())"'

audit: ## Dependency CVE scan (luxaudit, private tooling) — pinned locks vs the live advisory feed
	@if [ -n "$(LUXARCH_REGISTRY)" ]; then \
		docker run --rm -v $(PWD):/repo $(LUXAUDIT_IMAGE); \
	else \
		echo ">> luxaudit skipped: LUXARCH_REGISTRY unset (internal tooling — see Makefile.local.example)"; \
	fi

onboard-check: ## Prove all three guards are wired + HONEST (not green) — the fleet onboard gate
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> onboard-check skipped: LUXARCH_REGISTRY unset (internal tooling — see Makefile.local.example)"; exit 0; \
	fi
	@set +e; fail=0; \
	docker run --rm -v $(PWD):/repo $(LUXARCH_IMAGE)  --version   >/dev/null || { echo "luxarch not wired";  fail=1; }; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE)  --version   >/dev/null || { echo "luxlint not wired";  fail=1; }; \
	docker run --rm -v $(PWD):/repo $(LUXAUDIT_IMAGE) --version   >/dev/null || { echo "luxaudit not wired"; fail=1; }; \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE)  --preflight              || { echo "mypy tail NOT honest (luxlint --preflight)"; fail=1; }; \
	docker run --rm -v $(PWD):/repo $(LUXAUDIT_IMAGE) 2>&1 | grep -q "scan could not run" && { echo "luxaudit can't scan — supply-chain blind"; fail=1; }; \
	[ $$fail -eq 0 ] && echo "onboard-check: all three guards on + honest ✓" || { echo "onboard-check FAILED"; exit 1; }

hooks: ## Activate the repo git hooks (.githooks/) for this clone — run once after cloning
	git config core.hooksPath .githooks
	@echo "git hooks active (.githooks/): commit-msg blocks internal refs, pre-commit blocks infra hosts."

gitleaks: ## Scan the tree for committed secrets (run before the first git commit)
	docker run --rm -v $(PWD):/repo -w /repo ghcr.io/gitleaks/gitleaks:latest \
	  dir /repo/apps -c /repo/.gitleaks.toml --no-banner --redact -v

check: guard-version-check honest lint mypy test db-verify arch audit gitleaks ## THE fleet gate — run before every commit

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

test: test-build test-db-up ## Run the FULL backend suite (incl. durability) against the throwaway test DB (TEST=path/to/test for one file). Image is ALWAYS baked fresh from on-disk source (no :dev dependency).
	$(DC_TEST) run --rm tests pytest $(TEST) ; status=$$? ; $(MAKE) test-db-down ; exit $$status

test-db-up: ## Start the disposable test Postgres (throwaway — never the dev DB)
	$(DC_TEST) up -d --wait timescaledb-test

test-db-down: ## Stop + WIPE the disposable test Postgres
	@$(DC_TEST) down -v >/dev/null 2>&1 || true

test-down: test-db-down ## Tear down the test stack (alias for test-db-down)

db-verify: test-build test-db-up ## PROVE the migration chain builds the schema from EMPTY, no drift vs models
	@set -e; \
	$(DC_TEST) exec -T -e PGPASSWORD=luxswirl_test timescaledb-test \
	  psql -h 127.0.0.1 -U luxswirl_test -d postgres -v ON_ERROR_STOP=1 \
	  -c 'DROP DATABASE IF EXISTS luxswirl_migverify' \
	  -c 'CREATE DATABASE luxswirl_migverify'; \
	$(DC_TEST) run --rm \
	  -e DATABASE__URL=postgresql+asyncpg://luxswirl_test:luxswirl_test@timescaledb-test:5432/luxswirl_migverify \
	  tests sh -c 'alembic upgrade head && python scripts/verify_migration_chain.py'; \
	status=$$?; \
	$(DC_TEST) exec -T -e PGPASSWORD=luxswirl_test timescaledb-test \
	  psql -h 127.0.0.1 -U luxswirl_test -d postgres -c 'DROP DATABASE IF EXISTS luxswirl_migverify' >/dev/null 2>&1 || true; \
	$(MAKE) test-db-down; \
	exit $$status

migration-check: test-build ## Verify alembic migrations match the models — `alembic check` on a fresh DB must be clean (LUXSWIRL-178)
	$(DC_TEST) up -d --wait timescaledb-test
	$(DC_TEST) run --rm tests sh -c 'alembic upgrade head && alembic check' ; status=$$? ; $(DC_TEST) down -v ; exit $$status

# =============================================================================
# Utilities
# =============================================================================

##@ Utilities

# Regenerate the committed guard-status files (the fleet READS these instead of re-running).
# `|| true` on the run: --json exits non-zero when the repo is red — a red repo still has a
# valid, committable status. `set -e` so a failed stamp aborts (never a false "wrote").
STAMP = python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); d["commit"]=os.environ["SHA"]; d["generated_at"]=os.environ["TS"]; json.dump(d,open(sys.argv[2],"w"),indent=2)'

status: ## Regenerate committed guard-status files (.lux*-status.json) — commit them
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> status skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	@set -e; export SHA=$$(git rev-parse HEAD) TS=$$(date -u +%FT%TZ); \
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE)  --json > /tmp/lux.json || true; $(STAMP) /tmp/lux.json .luxlint-status.json; \
	docker run --rm -v $(PWD):/repo $(LUXARCH_IMAGE)  --json > /tmp/lux.json || true; $(STAMP) /tmp/lux.json .luxarch-status.json; \
	docker run --rm -v $(PWD):/repo $(LUXAUDIT_IMAGE) --json > /tmp/lux.json || true; $(STAMP) /tmp/lux.json .luxaudit-status.json; \
	echo "wrote .lux*-status.json at $$SHA — commit them"

ps: ## Show container status (dev stack)
	$(DC_DEV) ps

logs: ## Follow all logs (dev stack)
	$(DC_DEV) logs -f

clean: ## Remove stopped containers + orphans (dev stack)
	$(DC_DEV) down --remove-orphans

clean-all: ## Remove containers + volumes (DESTRUCTIVE — drops the DB)
	$(DC_DEV) down -v --remove-orphans
