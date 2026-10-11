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

# ── Images: the NAME declares what the image IS ──────────────────────────────
# Two axes, both readable from the name alone (repo.image_name_declares_provenance;
# see `luxarch --doc FLEET-BUILD-DEPLOY-STANDARD`):
#
#   Registry-qualified  ->  DEPLOY artifact  ->  built AND pushed
#   Bare (no registry)  ->  LOCAL artifact   ->  built from source, NEVER pushed
#
#   Immutable tag (:$(VERSION), :sha-…)  ->  a stack MAY pin it
#   Moving tag (:dev, :latest)           ->  a human alias; nothing pins it
#
# This repo publishes to a PUBLIC registry, so a local build must not wear a
# registry name: `$(REGISTRY)/…:dev` promised a ghcr artifact that was never
# pushed, and a `docker image prune` would have taken the dev stack down with
# nothing to re-pull. Local builds are bare, and they carry the commit sha so a
# stack pins something that cannot change under it.
REGISTRY        ?= ghcr.io
BACKEND_NAME    := luxardolabs/luxswirl-backend
AGENT_NAME      := luxardolabs/luxswirl-agent
# DEPLOY names — only build-backend/build-agent produce these, and both --push.
BACKEND_IMAGE   := $(REGISTRY)/$(BACKEND_NAME)
AGENT_IMAGE     := $(REGISTRY)/$(AGENT_NAME)
# LOCAL names — bare, so they cannot be pushed by accident or mistaken for a
# deployable. `$(notdir …)` strips the org: luxswirl-backend / luxswirl-agent.
BACKEND_LOCAL   := $(notdir $(BACKEND_NAME))
AGENT_LOCAL     := $(notdir $(AGENT_NAME))
# The immutable tag every local build wears. The sha is already computed above
# for BUILD_COMMIT, so this costs nothing and buys a deployment record: you can
# always say which bits a stack is running, and roll back to them.
SHA_TAG         := sha-$(COMMIT)

# Site config (gitignored) is read BEFORE the guard pins below, so it can set the
# registry host but never shadow a committed pin (repo.guard_pins_not_overridable).
-include Makefile.local

# Architecture guard (luxarch) — internal tooling on a private registry (not on ghcr,
# not part of this repo's public surface). Supply the registry host out-of-tree via
# Makefile.local (gitignored): copy Makefile.local.example -> Makefile.local and set it.
# Contributors don't need it — lint/test and the import-linter contracts run without it.
LUXARCH_REGISTRY ?=
LUXARCH_VERSION := 0.279.1
LUXARCH_IMAGE    ?= $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$(LUXARCH_VERSION)
# Dependency-CVE guard (luxaudit) — same private registry as luxarch, mount-only.
# Scans the pinned poetry.locks against the live advisory feed (see .luxaudit.toml).
LUXAUDIT_VERSION := 0.13.2
LUXAUDIT_IMAGE   ?= $(LUXARCH_REGISTRY)/luxardolabs/luxaudit:$(LUXAUDIT_VERSION)
# Lint/type/test-standard guard (luxlint) — same private registry. Owns the fleet
# ruff/mypy config byte-identical (see .luxlint.toml); ruff runs mount-only, mypy
# runs as an in-repo tail (needs a current mypy for PEP 758 excepts — the baked
# lint image freezes an older one, so the tail installs a fresh mypy).
LUXLINT_VERSION := 0.63.1
LUXLINT_IMAGE    ?= $(LUXARCH_REGISTRY)/luxardolabs/luxlint:$(LUXLINT_VERSION)
# The gitleaks asset reads the luxlint ref as $(LUXLINT).
LUXLINT          = $(LUXLINT_IMAGE)

# Multi-arch registry pushes (amd64 + arm64). Local build / build-dev stay single-
# arch — you can't --load a multi-platform image. Override with e.g. PLATFORMS=linux/amd64.
PLATFORMS       ?= linux/amd64,linux/arm64
# ONE shared fleet builder — never a per-project `<repo>-builder`. Ten per-project builders = ten
# un-shared caches (no base-layer/pip/npm dedup) = tens of GB idle; the shared builder dedups and
# cross-project cache-hits speed builds. Enforced by repo.shared_buildx_builder; see FLEET-BUILD-DEPLOY-STANDARD.

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
# The one Poetry: it wrote both locks, and every image installs the same one
# (repo.poetry_version_consistent).
POETRY_VERSION := 2.5.1
POETRY_SPEC := poetry==$(POETRY_VERSION)
# $(call poetry_cmd,<component-dir>,<poetry args>)
define poetry_cmd
docker run --rm -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD)/$(1):/work -w /work \
  python:3.14-slim sh -c 'python -m venv /tmp/v && /tmp/v/bin/pip install -q $(POETRY_SPEC) && /tmp/v/bin/poetry $(2)'
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

backend-image: ## Build the backend LOCALLY as $(BACKEND_LOCAL):$(VERSION) (bare name — never pushed)
	docker build $(NO_CACHE_FLAG) -f apps/backend/Dockerfile $(BUILD_ARGS) \
		-t $(BACKEND_LOCAL):$(VERSION) .

agent-image: ## Build the agent LOCALLY as $(AGENT_LOCAL):$(VERSION) (bare name — never pushed)
	docker build $(NO_CACHE_FLAG) -f apps/agent/Dockerfile $(BUILD_ARGS) \
		-t $(AGENT_LOCAL):$(VERSION) .

build: backend-image agent-image ## Build both LOCALLY as :$(VERSION) (bare names, no push)

# luxarch:buildx-setup asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit buildx-setup`.
BUILDX_BUILDER ?= luxardo-builder
buildx-setup:
	@mkdir -p $(HOME)/.docker
	@[ -f $(HOME)/.docker/buildkitd.toml ] || printf '[worker.oci]\n  gc = true\n  [[worker.oci.gcpolicy]]\n    keepBytes = "20GB"\n    all = true\n' > $(HOME)/.docker/buildkitd.toml
	@docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 || \
	  docker buildx create --name $(BUILDX_BUILDER) --driver docker-container \
	    --buildkitd-config $(HOME)/.docker/buildkitd.toml --use
	@docker buildx use $(BUILDX_BUILDER)
	@strays=$$(docker buildx ls 2>/dev/null | awk '$$2=="docker-container"{print $$1}' \
	  | grep -v '^\\_' | sed 's/\*$$//' | grep -vxF "$(BUILDX_BUILDER)" | tr '\n' ' '); \
	if [ -n "$$strays" ] && [ -z "$(ALLOW_STRAY_BUILDERS)" ]; then \
	  echo "REFUSING: stray per-project buildx builders are running: $$strays"; \
	  echo "Remove them:  docker buildx rm $$strays"; \
	  exit 1; \
	fi


# A release push is gated three ways, each BEFORE anything reaches the registry under a
# release name (repo.deploy_tag_is_immutable, repo.release_scans_candidate):
#   1. a dirty tree is refused — the tag names HEAD but the build reads the working dir;
#   2. an already-released :$(VERSION) is refused — prod pins it, so it is never re-pushed;
#      fails CLOSED: only the registry's own "not found" reads as unreleased;
#   3. the exact bits are scanned. Multi-arch can't --load, so the build pushes a
#      `candidate-<commit>` tag nothing pins, that tag is pulled + saved + scanned by luxaudit
#      (--image-archive), and only then are the release tags created FROM it, server-side.
define refuse_dirty_tree
	@if [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
	  echo "REFUSING: the working tree is dirty, so $(VERSION) / $(COMMIT) would not describe these bits:"; \
	  git status --short | sed 's/^/    /'; exit 1; \
	fi
endef

# $(call refuse_released,<image base>)
define refuse_released
	@if docker manifest inspect $(1):$(VERSION) >/dev/null 2>&1; then \
	  echo "REFUSING: $(1):$(VERSION) is already RELEASED. A released version is immutable: bump VERSION."; exit 1; \
	fi
	@out=$$(docker manifest inspect $(1):$(VERSION) 2>&1) || case "$$out" in \
	  *[Nn]"o such manifest"*|*"manifest unknown"*) ;; \
	  *) echo "REFUSING: cannot verify $(1):$(VERSION) is unreleased: $$out"; exit 1 ;; \
	esac
endef

# $(call scan_candidate,<image ref>) — luxaudit >= 0.13.0 scans the saved image, mount-only.
define scan_candidate
	@set -e; T=$$(mktemp); trap 'rm -f "$$T"' EXIT INT TERM; \
	docker save $(1) -o "$$T"; chmod 644 "$$T"; \
	docker run --rm -v $(PWD):/repo -v luxaudit-cache:/root/.cache/trivy -v "$$T":/candidate.tar:ro \
	  $(LUXAUDIT_IMAGE) --image-archive /candidate.tar --image-label $(1)
endef

# $(call publish_release,<image base>,<Dockerfile>)
define publish_release
	$(refuse_dirty_tree)
	$(call refuse_released,$(1))
	docker buildx build $(NO_CACHE_FLAG) --platform $(PLATFORMS) -f $(2) $(BUILD_ARGS) \
		-t $(1):candidate-$(COMMIT) --push .
	docker pull -q $(1):candidate-$(COMMIT)
	$(call scan_candidate,$(1):candidate-$(COMMIT))
	docker buildx imagetools create -t $(1):$(VERSION) -t $(1):latest $(1):candidate-$(COMMIT)
endef

build-backend: buildx-setup ## Build + scan + push backend :$(VERSION) AND :latest (multi-arch: $(PLATFORMS))
	$(call publish_release,$(BACKEND_IMAGE),apps/backend/Dockerfile)

build-agent: buildx-setup ## Build + scan + push agent :$(VERSION) AND :latest (multi-arch: $(PLATFORMS))
	$(call publish_release,$(AGENT_IMAGE),apps/agent/Dockerfile)

build-dev: ## Build both LOCALLY as :$(SHA_TAG) and pin .env.dev to it (bare names, no push)
	docker build $(NO_CACHE_FLAG) -f apps/backend/Dockerfile $(BUILD_ARGS) -t $(BACKEND_LOCAL):$(SHA_TAG) .
	docker build $(NO_CACHE_FLAG) -f apps/agent/Dockerfile $(BUILD_ARGS) -t $(AGENT_LOCAL):$(SHA_TAG) .
	@$(MAKE) --no-print-directory dev-pin TAG=$(SHA_TAG)

# An immutable tag moves a burden: a NEW tag per build has to actually REACH the
# stack. Without this, `build-dev` would leave the operator to hand-edit a
# gitignored file — advice beside a working button, and how a node ends up
# serving last week's build while everyone believes otherwise.
#
# Exactly ONE line of .env.<env> is rewritten in place; nothing else is read,
# printed or reordered, because the file holds secrets. The tag is PERSISTED
# rather than passed in the deploying shell, because the stack has to come back
# after a reboot — compose reads ${TAG:?}, so a tag that lived only in one shell
# leaves a plain `docker compose up` on that node unable to start at all.
# Generalised from the canonical `pin_env_tag` to take a KEY, because which
# images a LOCAL stack runs is a build-system fact, not an operator preference:
# dev and sandbox run the bare names this Makefile builds, and leaving that to a
# hand-edit is how a stack ends up silently pulling ghcr instead.
define pin_env_kv
	f='$(1)'; k='$(2)'; v='$(3)'; \
	[ -f "$$f" ] || { echo "$$f is missing — copy .env.example and fill it in first"; exit 1; }; \
	tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	if grep -qE "^[[:space:]]*$$k=" "$$f"; then \
	  awk -v k="$$k" -v v="$$v" '$$0 ~ "^[[:space:]]*"k"=" && !d {print k "=" v; d=1; next} {print}' "$$f" > "$$tmp"; \
	else \
	  cp "$$f" "$$tmp" && printf '%s=%s\n' "$$k" "$$v" >> "$$tmp"; \
	fi; \
	[ -s "$$tmp" ] || { echo "refusing to write an empty $$f"; exit 1; }; \
	o=$$(wc -l < "$$f"); n=$$(wc -l < "$$tmp"); \
	[ "$$n" -ge "$$o" ] || { echo "refusing: rewriting $$f lost lines ($$o -> $$n)"; exit 1; }; \
	cat "$$tmp" > "$$f"; \
	echo "$$f: $$k=$$v"
endef

# Point one local stack's env file at a built image set. Exactly three lines are
# rewritten in place; nothing else in the file is read, printed or reordered,
# because it holds secrets.
define pin_local_stack
	$(call pin_env_kv,$(1),SERVER_IMAGE,$(BACKEND_LOCAL)); \
	$(call pin_env_kv,$(1),AGENT_IMAGE,$(AGENT_LOCAL)); \
	$(call pin_env_kv,$(1),TAG,$(2))
endef

TAG ?= $(SHA_TAG)

dev-pin: ## Point .env.dev at an ALREADY-BUILT local tag (TAG=sha-… ; the rollback path)
	@docker image inspect $(BACKEND_LOCAL):$(TAG) >/dev/null 2>&1 || \
	  { echo "$(BACKEND_LOCAL):$(TAG) is not on this host — build it before pinning a stack to it"; exit 1; }
	@docker image inspect $(AGENT_LOCAL):$(TAG) >/dev/null 2>&1 || \
	  { echo "$(AGENT_LOCAL):$(TAG) is not on this host — build it before pinning a stack to it"; exit 1; }
	@$(call pin_local_stack,.env.dev,$(TAG))

sandbox-pin: ## Point .env.sandbox at an ALREADY-BUILT local tag (TAG=sha-…)
	@$(call pin_local_stack,.env.sandbox,$(TAG))

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
sandbox-up: build-dev ## Build the local images from current source, then start the isolated sandbox
	@$(MAKE) --no-print-directory sandbox-pin TAG=$(SHA_TAG)
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

lint: ## luxlint: the canonical ruff set, format, tests and docs standard (mount-only)
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> luxlint skipped: LUXARCH_REGISTRY unset (internal tooling — see Makefile.local.example)"; exit 0; \
	fi
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE)

format: ## THE canonical fixer — `luxlint --format` (ruff fix+format AND markdown), in place. PATH=<substr> to scope.
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> format skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --format $(if $(PATH_SCOPE),--path $(PATH_SCOPE),)

mypy: ## mypy for both components — `luxlint --mypy` (canonical config, fleet libraries baked, mount-only)
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> mypy skipped: LUXARCH_REGISTRY unset (internal tooling — see Makefile.local.example)"; exit 0; \
	fi
	docker run --rm -v $(PWD):/repo $(LUXLINT_IMAGE) --mypy

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

# The guard-upgrade asset pulls from $(REGISTRY). Here REGISTRY is the PUBLIC app registry
# (ghcr.io); the guards live on the private one, so this target reads that instead.
guard-upgrade: REGISTRY = $(LUXARCH_REGISTRY)
# luxarch:guard-upgrade asset v1 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit guard-upgrade`.
guard-upgrade:  ## Bump every guard pin to the published latest (prints what newly bites)
	@for g in luxarch luxlint luxaudit; do \
	  docker pull -q $(REGISTRY)/luxardolabs/$$g:latest >/dev/null 2>&1 || true; \
	  latest=$$(docker run --rm $(REGISTRY)/luxardolabs/$$g:latest --version 2>/dev/null | awk '{print $$2}'); \
	  var=$$(echo $$g | tr a-z A-Z)_VERSION; \
	  old=$$(sed -n -E "s/^$$var[[:space:]]*:=[[:space:]]*//p" Makefile); \
	  if [ -z "$$old" ]; then echo "!! no $$var pin found in Makefile — NOT bumped"; continue; fi; \
	  if [ -z "$$latest" ]; then echo "!! could not read $$g:latest — $$var left at $$old"; continue; fi; \
	  checked=1; \
	  sed -i -E "s|^($$var[[:space:]]*:=[[:space:]]*).*|\\1$$latest|" Makefile; \
	  new=$$(sed -n -E "s/^$$var[[:space:]]*:=[[:space:]]*//p" Makefile); \
	  if [ "$$new" != "$$latest" ]; then echo "!! $$var did NOT change (still $$new)"; exit 1; fi; \
	  if [ "$$old" != "$$latest" ]; then echo "$$var $$old -> $$latest"; bumped=1; fi; \
	  [ "$$g" = luxarch ] && [ "$$old" != "$$latest" ] && docker run --rm -v $(PWD):/repo $(REGISTRY)/luxardolabs/luxarch:$$latest --new-rules --since $$old || true; \
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

hooks: ## Activate the committed git hooks (hooks/) for this clone — run once after cloning
	git config core.hooksPath hooks
	@echo "git hooks active (hooks/): pre-commit = identity + gitleaks(staged), pre-push = gitleaks(full history + identity), commit-msg = no internal refs."

# luxarch:gitleaks asset v12 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit gitleaks`.
# ── The privacy gate: BOTH surfaces ─────────────────────────────────────────────────────────────
# Emitted by `luxarch --emit gitleaks`. Drop in verbatim.
#
# `gitleaks` scans DIFF CONTENT. A commit's author/committer address lives in the commit object
# HEADER and never appears in a patch, so no content rule can ever match it — it is a surface the
# scanner does not read. A repo reported `no leaks found` over 963 commits while 29 of them carried a
# personal address in both the author and committer fields, and it would have reported exactly the
# same thing after the scrub: identical output, opposite truth. Measured across the fleet, EIGHT
# repos carry a personal address in history and two of them are PUBLIC.

# Commit identities this repo accepts. The fleet account's `users.noreply.github.com` address, plus
# GitHub's own web-UI committer. Widen ONLY for a real outside contributor, with a comment saying who.
# NOT for the org account's real address: a role mailbox in commit metadata is published with every
# clone exactly like a personal one (six fleet repos carried it, one PUBLIC). Its
# omission here is the policy, not an oversight: the answer is the scrub printed below, and the
# repo's agent performs it once the OWNER approves the force-push.
# Anchored on the CLOSING BRACKET, because the compared line is `Name <email>` — not a bare
# address. The first cut allowed `^noreply@github.com$$`, which can NEVER match a
# `Name <email>` line, so the GitHub web-UI identity was silently DENIED and the canonical
# recipe would have refused on any repo carrying a web-UI commit. Measured across the fleet: it
# denied 4 of 6 distinct identity lines instead of the 3 real offenders.
# It was missed because the only repo it was tested on has no web-UI commits, so the broken
# branch never ran. The bracket also closes a substring hole: unanchored,
# `<x@users.noreply.github.com.attacker.test>` would have been allowed.
# v11: the noreply address is `<local@users.noreply.github.com>`, and the local part has no `@`. v10's
# `<[^>]*users…` admitted `<dev.real@gmail.com.users.noreply.github.com>`, a real address in the clear.
# v12: the fleet ACCOUNT, not any noreply address. v11 accepted `<anyone@users.noreply.github.com>`, so a
# stranger's (or a second account's) noreply committer passed. Measured: the fleet's whole history holds
# exactly two noreply identities, the fleet account and GitHub's web-UI committer.
GIT_IDENTITY_OK ?= <214140984\+luxardolabs@users\.noreply\.github\.com>$$|<noreply@github\.com>$$

# The secret scanner, PINNED and MIRRORED in the fleet registry. The fleet bans a moving tag
# everywhere it can see one, and this used to ship `ghcr.io/gitleaks/gitleaks:latest` inside the asset every
# repo adopts verbatim: the privacy gate could not run with ghcr unreachable or the local copy pruned, and
# nothing recorded which scanner said "no leaks found". New detection rules still arrive, through the fleet's
# own mechanism: luxarch bumps this pin in a release, and `repo.emitted_assets_current` tells you to re-emit.
# v5: the HOST is never written here. v4 inlined the private registry, so dropping
# this asset in "verbatim" put the host into a committed Makefile, and on a public repo the fleet's
# own gitleaks disclosure tier refused the commit. The mirror lives beside the guards, so the ref is
# derived from wherever this repo already pulls luxlint (`$(LUXLINT)`, which the scan below needs
# anyway). It works whichever variable holds your guard registry (REGISTRY, LUXARCH_REGISTRY, …).
# Recursive `=` so it resolves at use, whatever order LUXLINT is defined in.
# v9: PINNED BY DIGEST, and buildable off-network. The digest is the scanner's identity; the registry is
# only where it is fetched from. Beside a registry-qualified `$(LUXLINT)` it pulls the fleet mirror; with
# a local guard build (`luxlint:local`, on a machine with no access to the fleet registry, such as an
# airgapped laptop) it pulls the public image. v8 derived `./gitleaks:…` there, an unpullable reference, so the
# privacy gate could not run at all. The mirror and the public image share the digest, so both
# resolve to the same bits, and a tampered or re-tagged copy fails the pull instead of scanning.
GITLEAKS_IMAGE = $(if $(findstring /,$(LUXLINT)),$(dir $(LUXLINT)),zricethezav/)gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f

gitleaks: ## secret scan over FULL HISTORY + the commit-identity pass (the hooks cover commit/push)
	@set -e; C=$$(mktemp); trap 'rm -f "$$C"' EXIT INT TERM; \
	docker run --rm -v $(PWD):/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	docker run --rm -v $(PWD):/repo -v "$$C":/gl.toml:ro -w /repo \
	  $(GITLEAKS_IMAGE) git /repo -c /gl.toml --redact -v --ignore-gitleaks-allow
	@# v12: what `gitleaks git` never reads. It scans git's PATCHES, and git prints no patch for a file it
	@# treats as binary: a NUL byte, UTF-16 (a `Localizable.strings`), or a `binary` / `-diff` attribute.
	@# A token in any of them reached the remote with "no leaks found". So every path git ever showed as
	@# binary is re-read as text, NULs stripped, and scanned by path (the config's path allowlists hold).
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v $(PWD):/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	git -c core.quotePath=false log --branches --tags HEAD --format= -p --no-ext-diff --no-textconv \
	  > "$$D/patches"; \
	sed -n 's|^Binary files .* and b/\(.*\) differ$$|\1|p' "$$D/patches" | sort -u > "$$D/binary"; \
	if [ -s "$$D/binary" ]; then \
	  mkdir "$$D/t" "$$D/none"; \
	  while IFS= read -r f; do \
	    mkdir -p "$$D/t/$$(dirname "$$f")"; \
	    git -c core.quotePath=false log --branches --tags HEAD --format= -p --text --no-ext-diff \
	      --no-textconv -- "$$f" > "$$D/p"; \
	    grep -a '^+' "$$D/p" | tr -d '\000' > "$$D/t/$$f"; \
	  done < "$$D/binary"; \
	  docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	    $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	    --gitleaks-ignore-path /none; \
	fi
	@# The identity pass — the half gitleaks structurally cannot do. Cheap: one `git log`.
	@# Walks what THIS repo publishes (branches, tags, HEAD), NOT `--all`: a remote-tracking ref caches the
	@# remote's state, which during a scrub is by definition the un-rewritten history you are about to
	@# force-push over — `--all` refused the verified fix, and any `git fetch` re-armed it.
	@bad=$$(git log --branches --tags HEAD --pretty='%an <%ae>%n%cn <%ce>' 2>/dev/null | sort -u \
	  | grep -vE '$(GIT_IDENTITY_OK)' || true); \
	if [ -n "$$bad" ]; then \
	  echo "REFUSING: a non-fleet identity appears in commit METADATA (author/committer):"; \
	  echo "$$bad" | sed 's/^/    /'; \
	  echo "gitleaks cannot see this — it scans diffs, not commit headers, so it reported no leaks."; \
	  echo "An address here is attached to every affected commit forever, not to one line of one file."; \
	  echo "Scrub per FLEET-ONBOARDING-STANDARD §2: mirror backup -> git filter-repo -> re-verify with"; \
	  echo "  git log --branches --tags HEAD --pretty='%an <%ae>%n%cn <%ce>' | sort -u"; \
	  echo "-> ask the OWNER to approve the force-push, then do it yourself. Never force-push unapproved."; \
	  exit 1; \
	fi

# v6: the STAGED scan the commit hook calls (`hooks/pre-commit` → `make gitleaks-staged`) is part of the
# asset now. v5 shipped only the full-history half, so 9 of 10 adopting repos hand-wrote this target
# and the tenth had none, leaving its pre-commit hook pointing at a missing recipe. If your Makefile
# carries its own `gitleaks-staged`, delete it when you re-emit: this one replaces it.
# v7: `-w /repo` is LOAD-BEARING. Without it git runs outside the repo, falls back to `git diff
# --no-index`, rejects `--staged`, and gitleaks EXITS 0: v6 let a staged secret through while printing
# a git error (measured on a planted GitHub token: v6 exit 0, v7 "leaks found: 1" exit 1).
# v8: the denylist goes to a PER-RUN `mktemp` file, removed on exit. v7 wrote a fixed
# `/tmp/gl.toml` that outlived the run: on a host where commit and push run as different users, the
# next user's redirect was refused (`fs.protected_regular=1`, the Fedora default, blocks O_CREAT on
# another user's file in sticky /tmp even for root), so the privacy gate failed every commit or push
# after a user switch (2 of 2 measured). Two repos scanning at once also shared one file, so one could
# scan with the other's carve-outs. The full-history scan now also passes `-w /repo`, like the staged one.
# v12: the staged bytes are read on the HOST and scanned as files, by path. v11 ran `protect --staged`
# inside the container, which read `.git/index`, not the temporary index git hands the hook in
# `$$GIT_INDEX_FILE`: `git commit -a` and `git commit <path>` were never scanned. It also read git's
# patches, which skip binary, NUL, UTF-16 and `-diff` files, and it honoured a `# gitleaks:allow` on
# the secret's own line and a committed `.gitleaksignore`, so a commit's author could waive their own
# leak. New content gets no waiver: `.gitleaksignore` stays a ledger for the full-history scan only.
gitleaks-staged: ## secret scan of the STAGED changes (run by hooks/pre-commit)
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v $(PWD):/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	mkdir "$$D/t" "$$D/none"; \
	git -c core.quotePath=false diff --cached --name-only --diff-filter=ACMR -z > "$$D/names"; \
	tr '\000' '\n' < "$$D/names" | while IFS= read -r f; do \
	  [ -n "$$f" ] || continue; \
	  mkdir -p "$$D/t/$$(dirname "$$f")"; \
	  git -c core.quotePath=false diff --cached --text --no-ext-diff --no-textconv -U0 -- "$$f" \
	    > "$$D/p"; \
	  grep -a '^+' "$$D/p" | tr -d '\000' > "$$D/t/$$f"; \
	done; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none

check: guard-version-check honest lint mypy test db-verify arch audit gitleaks ## THE fleet gate — run before every commit

# The commit MESSAGE half of the privacy gate (hooks/commit-msg calls it). gitleaks reads patches,
# never messages, so the message is scanned as a file with the same fleet config — the internal
# names live only in luxlint's denylist, never spelled out in this public repo.
gitleaks-message: ## secret + denylist scan of one commit message (MSG=<file>; run by hooks/commit-msg)
	@[ -n "$(MSG)" ] && [ -f "$(MSG)" ] || { echo "usage: make gitleaks-message MSG=<file>"; exit 2; }
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v $(PWD):/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	mkdir "$$D/t" "$$D/none"; cp "$(MSG)" "$$D/t/COMMIT_MSG"; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact --no-banner --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none

docker-clean: ## Remove the locally built image tags (bare backend/agent + lint)
	-docker rmi $(BACKEND_LOCAL):$(SHA_TAG) $(AGENT_LOCAL):$(SHA_TAG) \
		$(BACKEND_LOCAL):$(VERSION) $(AGENT_LOCAL):$(VERSION) \
		$(BACKEND_LINT) $(AGENT_LINT) 2>/dev/null || true

# =============================================================================
# Tests (isolated test DB on tmpfs — mirrors luxwx)
# =============================================================================

##@ Tests

test-build: ## Build the test image from current source (self-contained — no :dev dependency)
	DOCKER_BUILDKIT=1 docker build $(NO_CACHE_FLAG) -f Dockerfile.test -t $(TEST_IMAGE) .

# Settings for the emitted test block below (set here, above it — never inside it).
TEST_SERVICES := timescaledb-test
TEST_WORKDIR  := apps/backend
# The suite reads TEST_DATABASE_URL (a name the app never reads, so an exported dev
# DATABASE__URL can never aim the schema-dropping harness at the dev DB); DATABASE__URL
# lands anything that builds the real engine on the throwaway DB too. PYTHONPATH points
# imports at the mounted source: `app.*` (apps/backend), `shared.*` (apps), `fixtures.*`.
# The source is mounted read-only, so Hypothesis keeps its example DB in the run's own dir.
TEST_ENV      := -e TEST_DATABASE_URL=postgresql+asyncpg://luxswirl_test:luxswirl_test@timescaledb-test:5432/luxswirl_test \
                 -e DATABASE__URL=postgresql+asyncpg://luxswirl_test:luxswirl_test@timescaledb-test:5432/luxswirl_test \
                 -e LUXSWIRL_INTEGRATION=1 \
                 -e PYTHONPATH=/repo/apps/backend:/repo/apps:/repo/apps/backend/tests \
                 -e HYPOTHESIS_STORAGE_DIRECTORY=/out/.hypothesis

# luxarch:test-block asset v1 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit test-block`.
# ── Test: THE suite, in the test image, against an isolated stack of real services ───────────────
# Emitted by `luxarch --emit test-block`; paste below the image block (it uses TEST_IMAGE and
# test-build from there). Enforced by `repo.test_block_wired`. Settings are `?=` defaults: set them
# above this block. What the suite can see, and what only `make smoke` sees: --doc
# FLEET-MAKEFILE-STANDARD §1.
#
# Before this block every repo wrote its own `make test`: four repos, four ways (a lint image with
# the source mounted, the dev image with pytest pip-installed at run time, a repo script, a test
# image), each with its own readiness loop and coverage wiring, and a coverage pipe under make's
# /bin/sh that hid pytest's failure. This is the documented practice of the tools instead:
#   - ISOLATED STACK (Docker Compose): the backing services run in a compose project of their own,
#     one per run (`-p`), so parallel runs never collide, the suite cannot reach the dev database or
#     cache at all (it is on another network), and teardown (`down --volumes --remove-orphans`)
#     removes exactly this run's containers and data, pass or fail, never the dev stack.
#   - READINESS from each service's own compose healthcheck (`up --wait`), not a sleep or a loop:
#     the service declares when it is ready. For Postgres, probe over TCP with the real role
#     (`pg_isready -h 127.0.0.1 -U <user> -d <db>`); over the socket it answers while initdb's
#     temporary server is still up. `repo.test_stack_parity` checks these are the services prod runs.
#   - Ctrl-C stops the suite (`--init` forwards the signal; a shell as PID 1 ignores it).
#   - THE TEST IMAGE built from this source (`test-build`, the image's `--with dev` stage), with the
#     fleet's pytest config (`luxlint --emit-config pytest`: -ra, strict markers and config,
#     warnings are errors), readable by the image's non-root user. The source is mounted read-only
#     and pytest writes no cache into it.
#   - COVERAGE with coverage.py itself, not pytest-cov: `coverage run --branch` under the sysmon
#     core (fast branch coverage on Python 3.14), data in /tmp, then `coverage report` judged by
#     `luxlint --coverage-ratchet` against `[test].coverage_min` (off until you set a floor; it
#     only ratchets up). `coverage` belongs in the dev dependency group.
#   - BOTH EXIT CODES reach make: pytest's and the ratchet's. No pipe carries either. The ratchet
#     reads coverage's own report file, never the suite's output (a printed `TOTAL … 100%` or
#     pytest's `[100%]` would otherwise pass for a measurement), and a report that measured nothing
#     fails.
#   - Each run's project is named from its own `mktemp -d` token, and refuses to run without one (a
#     PID repeats across containers and CI runners), and everything mounts the makefile's directory ($(CURDIR)), so `make -C` runs the
#     right suite.

# Setting: the compose command, and the profile holding the test services --------------------
TEST_COMPOSE ?= docker compose
TEST_PROFILE ?= test
# Setting: the env file compose interpolates the file with (it reads EVERY service, so an app
# service's `${TAG:?}` needs a value even when only the test services start) -----------------
TEST_ENV_FILE ?= $(firstword $(wildcard .env.test .env.dev .env.example))
# Setting: the backing services the suite needs (each with a healthcheck); empty: none -----------
TEST_SERVICES ?= db-test
# Setting: the suite's environment: the test services' URLs, by service name on the test network -
TEST_ENV ?= -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@db-test:5432/postgres
# Setting: where the suite runs from (a monorepo's apps/backend), and every package coverage
# measures, comma-separated (`app,collector`): it replaces any [tool.coverage.run] source ---------
TEST_WORKDIR ?= .
TEST_COV ?= app
# Setting: NONE. For a one-off run only, on the command line: `make test PYTEST_ARGS='-k orders'`.
# A committed value narrows THE suite for everyone (`repo.test_block_wired` reds one) ---------
PYTEST_ARGS ?=

# One-off pytest arguments reach the container through the environment, never spliced into a quoted
# command line (`-k 'a or b'` would otherwise split it).
export PYTEST_ARGS

test: test-build ## THE suite: test image, an isolated stack of real services, coverage ratchet
	@set -u; \
	D=$$(mktemp -d); tok=$$(basename "$$D" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9'); \
	[ $${#tok} -ge 8 ] || { echo "REFUSING: could not make a unique name for this run"; rm -rf "$$D"; exit 2; }; \
	run="t$$(printf '%s' '$(notdir $(CURDIR))' | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')-test-$$tok"; \
	dc="$(TEST_COMPOSE) -p $$run $(if $(TEST_ENV_FILE),--env-file $(TEST_ENV_FILE)) --profile $(TEST_PROFILE)"; \
	chmod 777 "$$D"; \
	trap '$$dc down --volumes --remove-orphans >/dev/null 2>&1; rm -rf "$$D"' EXIT INT TERM; \
	net=; if [ -n "$(TEST_SERVICES)" ]; then \
	  $$dc up -d --wait --wait-timeout 120 $(TEST_SERVICES) \
	    || { echo "FAIL  the test services did not become healthy: $(TEST_SERVICES)"; exit 1; }; \
	  cid=$$($$dc ps -q $(firstword $(TEST_SERVICES))); net=; \
	  for n in $$(docker inspect -f '{{range $$k, $$v := .NetworkSettings.Networks}}{{$$k}} {{end}}' $$cid); do \
	    [ "$$(docker network inspect -f '{{index .Labels "com.docker.compose.project"}}' $$n)" = "$$run" ] && { net=$$n; break; }; done; \
	  [ -n "$$net" ] || { echo "FAIL  $(firstword $(TEST_SERVICES)) joined no network of this run's own project ($$run)"; exit 1; }; \
	  net="--network $$net"; fi; \
	docker run --rm -v $(CURDIR):/repo $(LUXLINT) --emit-config pytest > "$$D/pytest.ini" || exit 2; \
	chmod 644 "$$D/pytest.ini"; \
	docker run --rm --init $$net $(TEST_ENV) -e PYTEST_ADDOPTS="$${PYTEST_ARGS:-}" \
	  -e COVERAGE_CORE=sysmon -e COVERAGE_FILE=/out/.coverage -e PYTHONDONTWRITEBYTECODE=1 \
	  -v $(CURDIR):/repo:ro -v "$$D":/out -w /repo/$(TEST_WORKDIR) $(TEST_IMAGE) \
	  sh -c 'python -m coverage run --branch --source=$(TEST_COV) -m pytest -c /out/pytest.ini --rootdir=. -p no:cacheprovider; s=$$?; python -m coverage report --show-missing > /out/coverage.txt; echo $$? > /out/coverage.rc; cat /out/coverage.txt; exit $$s'; \
	rc=$$?; \
	[ "$$rc" = 0 ] || { echo "FAIL  the suite failed (exit $$rc)"; exit 1; }; \
	[ "$$(cat "$$D/coverage.rc" 2>/dev/null)" = 0 ] || { echo "FAIL  coverage measured nothing (coverage report: $$(tail -n 1 "$$D/coverage.txt" 2>/dev/null)): check TEST_COV names the package the suite imports"; exit 1; }; \
	docker run --rm -i -v $(CURDIR):/repo $(LUXLINT) --coverage-ratchet < "$$D/coverage.txt" > "$$D/ratchet.txt"; crc=$$?; \
	sed -n '/coverage ratchet/,$$p' "$$D/ratchet.txt"; \
	[ "$$crc" = 0 ] || { echo "FAIL  the coverage ratchet failed (its verdict is above): add tests, never lower the floor"; exit 1; }

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
STAMP = python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); g=d.get("guard"); g==sys.argv[3] or sys.exit(f"status: {sys.argv[1]} holds {g!r} output, expected {sys.argv[3]!r}; refusing to stamp"); d["commit"]=os.environ["SHA"]; d["generated_at"]=os.environ["TS"]; json.dump(d,open(sys.argv[2],"w"),indent=2)'
GUARD_RUN = docker run --rm -v $(PWD):/repo

# LUXARCH_STATUS_WRITE=1 tells luxarch that THIS run is the one writing the
# snapshot, so repo.guard_status_current does not judge the files it is about to
# replace. `mktemp`: a per-run file, never a fixed /tmp path every repo on the host shares.
status: ## Regenerate committed guard-status files (.lux*-status.json) — commit them
	@if [ -z "$(LUXARCH_REGISTRY)" ]; then \
		echo ">> status skipped: LUXARCH_REGISTRY unset (see Makefile.local.example)"; exit 0; \
	fi
	@set -e; export SHA=$$(git rev-parse HEAD) TS=$$(date -u +%FT%TZ); \
	J=$$(mktemp); trap 'rm -f "$$J"' EXIT INT TERM; \
	$(GUARD_RUN) $(LUXLINT_IMAGE)  --json > "$$J" || true; $(STAMP) "$$J" .luxlint-status.json luxlint; \
	$(GUARD_RUN) -e LUXARCH_STATUS_WRITE=1 $(LUXARCH_IMAGE) --json > "$$J" || true; $(STAMP) "$$J" .luxarch-status.json luxarch; \
	$(GUARD_RUN) $(LUXAUDIT_IMAGE) --json > "$$J" || true; $(STAMP) "$$J" .luxaudit-status.json luxaudit; \
	echo "wrote .lux*-status.json at $$SHA — commit them"

ps: ## Show container status (dev stack)
	$(DC_DEV) ps

logs: ## Follow all logs (dev stack)
	$(DC_DEV) logs -f

clean: ## Remove stopped containers + orphans (dev stack)
	$(DC_DEV) down --remove-orphans

clean-all: ## Remove containers + volumes (DESTRUCTIVE — drops the DB)
	$(DC_DEV) down -v --remove-orphans
