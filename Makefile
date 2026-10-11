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
LUXARCH_VERSION := 0.281.0
LUXARCH_IMAGE    ?= $(LUXARCH_REGISTRY)/luxardolabs/luxarch:$(LUXARCH_VERSION)
# Dependency-CVE guard (luxaudit) — same private registry as luxarch, mount-only.
# Scans the pinned poetry.locks against the live advisory feed (see .luxaudit.toml).
LUXAUDIT_VERSION := 0.14.0
LUXAUDIT_IMAGE   ?= $(LUXARCH_REGISTRY)/luxardolabs/luxaudit:$(LUXAUDIT_VERSION)
# Lint/type/test-standard guard (luxlint) — same private registry. Owns the fleet
# ruff/mypy config byte-identical (see .luxlint.toml); ruff runs mount-only, mypy
# runs as an in-repo tail (needs a current mypy for PEP 758 excepts — the baked
# lint image freezes an older one, so the tail installs a fresh mypy).
LUXLINT_VERSION := 0.65.0
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

# Smoke settings for this repo (the block below reads them). The backend image
# is the one whose entry points are imported; the agent is probed through its
# container (build revision + its own HEALTHCHECK). SMOKE_URL is the dev
# stack's address and belongs in Makefile.local (see Makefile.local.example).
SHA_IMAGE          := $(BACKEND_LOCAL):$(SHA_TAG)
SMOKE_CONTAINERS   ?= luxswirl_server luxswirl_agent
SMOKE_SCRIPTS_DIR  := apps/backend/app/scripts
SMOKE_IMPORT_ROOT  := apps/backend
SMOKE_ENTRY_MODULES := app.main
SMOKE_AUTH_PATH    ?= /api/v1/agents

# luxarch:smoke asset v7 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit smoke`.
# ── Smoke: probe the DEPLOYED stack from outside, after every dev deploy ──────────────────────────
# Emitted by `luxarch --emit smoke`; paste below the image block. `make test` runs the app in-process
# against its test stack, so it cannot see the proxy, the server, the production image or an entry
# point other than the app's. One repo shipped four bugs under a green `make check` that only a probe
# of the deployed stack found: a rate limiter that took the host down, a proxy serving an unstyled UI,
# a standalone script that crashed on a circular import in the production image, and a Host-header
# defence no test exercised. Each probe below is aimed at one of them. `make dev-deploy` runs it
# (`repo.stack_smoke_wired`); a FAIL fails the deploy, and a probe that cannot run says NOT RUN and
# why, never a silent pass.
#
# Settings are `?=` defaults: override them above this block (or in Makefile.local for a host name).

# Setting: where the dev stack is reachable from the build host (e.g. https://dev.example.com) ---
SMOKE_URL ?=
# Setting: the deployed containers to probe on the docker host the deploy restarted. Each must run
# THIS build (its image's org.opencontainers.image.revision label names the commit) and reach
# `healthy` by its own HEALTHCHECK. For a stack that serves no HTTP (a collector, a poller) this is
# the probe, and SMOKE_URL stays empty; an HTTP stack may list its containers too (v5) ----------
SMOKE_CONTAINERS ?=
# Setting: how many seconds a container may stay `starting` before its health probe FAILs (v5) --
SMOKE_HEALTH_WAIT ?= 180
# Setting: how many seconds every container must STAY healthy, with no restart, after its first
# healthy sample; a HEALTHCHECK with a longer interval stretches it to one interval, so that check
# has run again (v6) ------------------------------------------------------------------------------
SMOKE_HEALTH_SETTLE ?= 30
# Setting: the health path; its response must name the build's commit (any field name) -------
SMOKE_HEALTH_PATH ?= /health
# Setting: a static asset the image serves (empty only if the app serves no static files) -----
SMOKE_STATIC_PATH ?= /static/css/app.css
# Setting: a path that needs authentication; the token comes from the SMOKE_AUTH_TOKEN env var -
SMOKE_AUTH_PATH ?=
# Setting: the package directory of standalone entry points, and the image's import root -------
SMOKE_SCRIPTS_DIR ?= app/scripts
SMOKE_IMPORT_ROOT ?= .
# Setting: entry-point MODULES outside that directory. The default is every `*_main` module at the
# app package's root, where the layout standard (fw.module_homes) puts a service's entry points
# (`app.portal_main`); list them yourself for any other place (v3) ---------------------------
SMOKE_ENTRY_MODULES ?= $(basename $(subst /,.,$(patsubst $(SMOKE_IMPORT_ROOT)/%,%,$(wildcard $(SMOKE_IMPORT_ROOT)/app/*_main.py))))
# Setting: the env file the dev stack runs with; each entry point is imported with it, because a
# module that builds the app's settings needs the config the deployed container has (v2). Its
# `TAG=` line names the build the deploy pinned, and that is the build every probe expects (v6) --
SMOKE_ENV_FILE ?= .env.dev
# Setting: extra `docker run` options for the entry-point imports (e.g. -e NAME=value) ----------
SMOKE_RUN_OPTS ?=
# Setting: extra curl options (e.g. --cacert <file> for a private CA) -------------------------
SMOKE_CURL_OPTS ?=

# v7: git's own error is shown when the commit cannot be read. v6 discarded it, so a checkout owned by
# another user (git's "dubious ownership" refusal) printed only "git rev-parse failed". Trusting the
# checkout is the operator's call, made once with git's own remedy, never bypassed here: the image
# block's dirty-tree check refuses the same checkout for the same reason.
# v6: a forged Host that is answered names the defence to install (FLEET-BUILD-DEPLOY-STANDARD "Trusted hosts"):
# v5 said the defence was off and nothing said what it is, so each repo invented its own.
# v6: one healthy sample was enough, so a container that crashed and restarted right after it PASSed.
# A container now passes only when it is still healthy SMOKE_HEALTH_SETTLE seconds later (at least
# one interval of its HEALTHCHECK) with the same start time and restart count. The build expected is the one SMOKE_ENV_FILE's `TAG=` pins: v5
# compared against HEAD only, so a `make dev-pin TAG=<release>` rollback could never be verified.
# A forged-Host 502 behind a front proxy names its likely cause; a refusal is only credited when the
# same URL answers the real Host (a stack that answers nothing does not refuse anything).
# v5: a stack with no HTTP surface has nothing for the HTTP probes to ask, and v4 refused to run at all
# without SMOKE_URL. It is probed through its containers instead (SMOKE_CONTAINERS): the build each
# runs and the verdict of its own HEALTHCHECK, beside the entry-point imports.
# v4: a 502/503/504 on the forged-Host probe is the PROXY answering for an app that did not, not a refusal.
# v3 printed "PASS  forged Host refused (502)" while the stack was still starting, beside two FAILs that
# said the same thing; the probe can only pass on an answer from the app itself.
smoke: ## Probe the deployed dev stack: build commit, static asset, forged Host refused, auth, containers healthy, standalone entry points
	@set -u; fail=0; \
	[ -n "$(strip $(SMOKE_URL)$(SMOKE_CONTAINERS))" ] || { echo "REFUSING: set SMOKE_URL to the dev stack's address (Makefile.local), or SMOKE_CONTAINERS to its containers when it serves no HTTP"; exit 2; }; \
	probe() { curl -sS --max-time 15 $(SMOKE_CURL_OPTS) -o "$$B" -w '%{http_code} %{content_type}' "$$@" 2>/dev/null || echo "000 -"; }; \
	B=$$(mktemp); trap 'rm -f "$$B"' EXIT INT TERM; \
	sha=$$(git rev-parse --short=7 HEAD) || sha=; \
	pin=; if [ -n "$(SMOKE_ENV_FILE)" ] && [ -f "$(SMOKE_ENV_FILE)" ]; then pin=$$(sed -n 's/^[[:space:]]*TAG=//p' "$(SMOKE_ENV_FILE)" | tail -n 1 | tr -d "\"' \r"); fi; \
	want=$$sha; what="this commit"; img="$(SHA_IMAGE)"; \
	if [ -n "$$pin" ] && { [ -z "$$sha" ] || case "$$pin" in sha-"$$sha"*) false;; *) true;; esac; }; then \
	  img="$(IMAGE):$$pin"; what="the pinned TAG=$$pin"; want=; \
	  if ! docker image inspect "$$img" >/dev/null 2>&1 && ! docker pull -q "$$img" >/dev/null 2>&1; then echo "FAIL  $(SMOKE_ENV_FILE) pins TAG=$$pin, and $$img is neither here nor pullable, so the build the stack should run cannot be named"; fail=1; \
	  else want=$$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$$img" 2>/dev/null | cut -c1-7); \
	    if [ -z "$$want" ]; then echo "FAIL  $$img carries no org.opencontainers.image.revision label, so the build the stack should run cannot be named"; fail=1; \
	    else echo "NOTE  $(SMOKE_ENV_FILE) pins TAG=$$pin (revision $$want), not this checkout ($${sha:-unknown}): probing the pinned build"; fi; fi; \
	elif [ -z "$$sha" ]; then echo "FAIL  cannot read this checkout's commit (git's error is above; a checkout owned by another user needs git config --global --add safe.directory $(CURDIR)), so the deployed build cannot be checked"; fail=1; fi; \
	if [ -z "$(SMOKE_URL)" ]; then echo "NOT RUN  HTTP probes (health, static asset, forged Host, auth): SMOKE_URL is empty, so the stack is probed only through SMOKE_CONTAINERS (right only for a stack that serves no HTTP)"; \
	else \
	r=$$(probe "$(SMOKE_URL)$(SMOKE_HEALTH_PATH)"); hcode=$${r%% *}; \
	if [ -z "$$want" ]; then :; \
	elif [ "$$hcode" = 200 ] && grep -q "$$want" "$$B"; then echo "PASS  health names $$what ($$want)"; \
	else echo "FAIL  $(SMOKE_HEALTH_PATH): $$r, and the response does not name $$want: the stack is not running $$what"; fail=1; fi; \
	if [ -n "$(SMOKE_STATIC_PATH)" ]; then \
	  r=$$(probe "$(SMOKE_URL)$(SMOKE_STATIC_PATH)"); ctype=$${r#* }; \
	  case "$(SMOKE_STATIC_PATH)" in *.css) want=text/css;; *.js|*.mjs) want=javascript;; *) want=;; esac; \
	  if [ "$${r%% *}" = 200 ] && [ -s "$$B" ] && ! grep -qi '<html' "$$B" && { [ -z "$$want" ] || case "$$ctype" in *"$$want"*) true;; *) false;; esac; }; then echo "PASS  static asset served ($(SMOKE_STATIC_PATH), $$ctype)"; \
	  else echo "FAIL  $(SMOKE_STATIC_PATH): $$r: the proxy or image does not serve the built asset as $${want:-a file} (a browser refuses a stylesheet or script with the wrong type)"; fail=1; fi; \
	else echo "NOT RUN  static asset: SMOKE_STATIC_PATH is empty (only right for an app that serves no static files)"; fi; \
	r=$$(probe -H "Host: smoke-forged.invalid" "$(SMOKE_URL)$(SMOKE_HEALTH_PATH)"); \
	case "$${r%% *}" in 2??|3??) echo "FAIL  a forged Host header was answered ($$r): the trusted-host defence is not on in the real stack. The app must refuse it: luxarch --doc FLEET-BUILD-DEPLOY-STANDARD, \"Trusted hosts\" (luxarch --emit trusted-host)"; fail=1;; \
	  000) if [ "$$hcode" = 000 ]; then echo "FAIL  the forged-Host probe got no answer, and neither did $(SMOKE_HEALTH_PATH) with the real Host: nothing is answering at $(SMOKE_URL), so whether it refuses a forged Host is unknown"; fail=1; \
	    else echo "PASS  forged Host refused (connection rejected)"; fi;; \
	  502|503|504) if [ "$$hcode" = 200 ]; then echo "FAIL  the forged-Host probe got $${r%% *} while $(SMOKE_HEALTH_PATH) answers 200 through the same URL: a proxy in front of the stack reported a refusal it could not pass on (a closed connection: nginx's 444) as $${r%% *}, or the stack failed this request. Either way no refusal was seen. Behind another proxy, refuse with an answer: the app's trusted-host check answers 400 through any proxy, so let the forged Host reach it, or have the catch-all server return 400, not 444. Keep SMOKE_URL on the public address: FLEET-BUILD-DEPLOY-STANDARD, \"Trusted hosts\""; \
	    else echo "FAIL  the forged-Host probe got $${r%% *} from the proxy: the app did not answer, so whether it refuses a forged Host is unknown"; fi; fail=1;; \
	  *) echo "PASS  forged Host refused ($${r%% *})";; esac; \
	if [ -n "$(SMOKE_AUTH_PATH)" ]; then \
	  r=$$(probe "$(SMOKE_URL)$(SMOKE_AUTH_PATH)"); \
	  case "$${r%% *}" in 401|403) echo "PASS  $(SMOKE_AUTH_PATH) refuses a request with no credentials ($${r%% *})";; \
	    *) echo "FAIL  $(SMOKE_AUTH_PATH) answered a request with NO credentials ($$r): it is not protected"; fail=1;; esac; \
	  if [ -z "$${SMOKE_AUTH_TOKEN:-}" ]; then echo "FAIL  SMOKE_AUTH_PATH is set but SMOKE_AUTH_TOKEN is not in the environment"; fail=1; \
	  else r=$$(probe -H "Authorization: Bearer $${SMOKE_AUTH_TOKEN}" "$(SMOKE_URL)$(SMOKE_AUTH_PATH)"); \
	    if [ "$${r%% *}" = 200 ]; then echo "PASS  authenticated request ($(SMOKE_AUTH_PATH))"; \
	    else echo "FAIL  authenticated $(SMOKE_AUTH_PATH): $$r"; fail=1; fi; fi; \
	else echo "NOT RUN  authenticated request: SMOKE_AUTH_PATH is empty"; fi; \
	fi; \
	settle=$(SMOKE_HEALTH_SETTLE); healthy=; \
	for c in $(SMOKE_CONTAINERS); do \
	  if ! rev=$$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$$c" 2>/dev/null); then echo "FAIL  container $$c is not on this docker host: the deploy did not bring it up"; fail=1; continue; fi; \
	  if [ -n "$$want" ]; then case "$$rev" in *"$$want"*) echo "PASS  $$c runs $$what ($$want)";; \
	    *) echo "FAIL  $$c runs revision '$$rev', not $$want: the stack is not running $$what (an image with no org.opencontainers.image.revision label cannot say which build it is)"; fail=1;; esac; fi; \
	  life=$$(docker inspect -f '{{.RestartCount}} {{.State.StartedAt}}' "$$c" 2>/dev/null || echo gone); \
	  t=0; while :; do \
	    st=$$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$$c" 2>/dev/null || echo "gone -"); \
	    case "$$st" in "running starting"|restarting*) [ "$$t" -lt $(SMOKE_HEALTH_WAIT) ] || break; sleep 5; t=$$((t + 5));; *) break;; esac; \
	  done; \
	  case "$$st" in "running healthy") \
	    iv=$$(docker inspect -f '{{if .Config.Healthcheck}}{{json .Config.Healthcheck.Interval}}{{end}}' "$$c" 2>/dev/null); \
	    case "$$iv" in ""|*[!0-9]*) iv=0;; esac; iv=$$((iv / 1000000000)); [ "$$iv" -gt 0 ] || iv=30; \
	    [ "$$iv" -le "$$settle" ] || settle=$$iv; healthy="$$healthy $$c=$$(printf '%s' "$$life" | tr ' ' '@')";; \
	    "running none") echo "FAIL  $$c declares no HEALTHCHECK, so nothing shows its process works: declare one in its Dockerfile or compose service"; fail=1;; \
	    "running starting") echo "FAIL  $$c is still starting after $(SMOKE_HEALTH_WAIT)s: its HEALTHCHECK never passed"; fail=1;; \
	    "running unhealthy") why=$$(docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$$c" 2>/dev/null | grep -v '^[[:space:]]*$$' | tail -n 1); echo "FAIL  $$c is unhealthy by its own HEALTHCHECK: $$why"; fail=1;; \
	    *) echo "FAIL  $$c is not running ($$st): the deployed process stopped or is crash-looping"; fail=1;; esac; \
	done; \
	if [ -n "$$healthy" ]; then sleep "$$settle"; for e in $$healthy; do \
	  c=$${e%%=*}; life=$$(printf '%s' "$${e#*=}" | tr '@' ' '); \
	  st=$$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$$c" 2>/dev/null || echo "gone -"); \
	  now=$$(docker inspect -f '{{.RestartCount}} {{.State.StartedAt}}' "$$c" 2>/dev/null || echo gone); \
	  if [ "$$now" != "$$life" ]; then echo "FAIL  $$c restarted during the probe (restart count and start time '$$life', then '$$now'): it crashed after its HEALTHCHECK passed"; fail=1; \
	  else case "$$st" in "running healthy") echo "PASS  $$c is healthy by its own HEALTHCHECK, and still is $${settle}s later with no restart";; \
	    "running unhealthy") why=$$(docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$$c" 2>/dev/null | grep -v '^[[:space:]]*$$' | tail -n 1); echo "FAIL  $$c is unhealthy by its own HEALTHCHECK $${settle}s after it first passed: $$why"; fail=1;; \
	    *) echo "FAIL  $$c is not running ($$st) $${settle}s after its HEALTHCHECK passed: the deployed process stopped"; fail=1;; esac; fi; \
	done; fi; \
	n=0; mods=; \
	if [ -d "$(SMOKE_SCRIPTS_DIR)" ]; then for f in $$(find "$(SMOKE_SCRIPTS_DIR)" -name '*.py' ! -name '__init__.py' | sort); do \
	  rel=$$(realpath --relative-to="$(SMOKE_IMPORT_ROOT)" "$$f"); mods="$$mods $$(printf '%s' "$${rel%.py}" | tr / .)"; done; fi; \
	mods=$$(printf '%s\n' $$mods $(SMOKE_ENTRY_MODULES) | sort -u); \
	if [ -n "$$mods" ]; then \
	  envf=; [ -n "$(SMOKE_ENV_FILE)" ] && [ -f "$(SMOKE_ENV_FILE)" ] && envf="--env-file=$(SMOKE_ENV_FILE)"; \
	  if ! docker image inspect "$$img" >/dev/null 2>&1 && ! docker pull -q "$$img" >/dev/null 2>&1; then \
	    echo "FAIL  entry points: $$img is neither built here nor pullable, so no module could be imported (deploy this commit first)"; fail=1; n=-1; \
	  else for mod in $$mods; do \
	    n=$$((n + 1)); \
	    if err=$$(docker run --rm $$envf $(SMOKE_RUN_OPTS) --entrypoint python "$$img" -c "import $$mod" 2>&1 >/dev/null); then echo "PASS  $$mod imports on its own in the production image"; \
	    else fail=1; why=$$(printf '%s\n' "$$err" | grep -E '^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Exit)\b' | tail -n 1); why=$${why:-$$(printf '%s\n' "$$err" | grep -v '^[[:space:]]*$$' | tail -n 1)}; \
	      case "$$err" in *ImportError*|*ModuleNotFoundError*|*"circular import"*) echo "FAIL  $$mod does not import on its own in $$img (a circular or missing import the app's own import order hides): $$why";; \
	        *) echo "FAIL  $$mod raised at import in $$img: $$why"; [ -n "$$envf" ] || echo "      no env file was passed ($(SMOKE_ENV_FILE) not found): set SMOKE_ENV_FILE to the file the stack runs with";; esac; fi; \
	  done; fi; \
	fi; \
	[ "$$n" -ne 0 ] || echo "NOT RUN  entry points: no module under $(SMOKE_SCRIPTS_DIR) and no SMOKE_ENTRY_MODULES"; \
	exit $$fail

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

# A deploy ends by probing what it deployed (FLEET-BUILD-DEPLOY-STANDARD, "Stack
# smoke"): `up --wait` holds until every service's HEALTHCHECK passes, so the
# smoke's first probe reaches the app rather than a proxy still waiting on it.
DEV_WAIT_TIMEOUT ?= 180
dev-deploy: ## Build THIS commit (:sha-…), pin .env.dev to it, restart the dev stack, smoke it
	@$(MAKE) --no-print-directory build-dev
	$(DC_DEV) up -d --wait --wait-timeout $(DEV_WAIT_TIMEOUT)
	@$(MAKE) --no-print-directory smoke

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

css: ## Build the CSS locally (gitignored; the image compiles its own) — in docker, no host node
	docker run --rm -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD)/apps/backend:/work -w /work \
	  node:24-slim sh -c 'npm ci --no-audit --no-fund && npm run build:css'

css-watch: ## Recompile the CSS on change (gitignored output) — in docker
	docker run --rm -it -u $(REPO_UID):$(REPO_GID) -e HOME=/tmp -v $(PWD)/apps/backend:/work -w /work \
	  node:24-slim sh -c 'npm ci --no-audit --no-fund && npm run build:css -- --watch'

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
# luxarch:guard-upgrade asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit guard-upgrade`.
# v2: the preview mounts `$(CURDIR)`, this makefile's directory (it edits ./Makefile there). v1 mounted
# `$(PWD)`, the caller's directory, so `make -C <repo> guard-upgrade` previewed ANOTHER repo's new reds.
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
	  [ "$$g" = luxarch ] && [ "$$old" != "$$latest" ] && docker run --rm -v "$(CURDIR)":/repo $(REGISTRY)/luxardolabs/luxarch:$$latest --new-rules --since $$old || true; \
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

# luxarch:gitleaks asset v13 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit gitleaks`.
# ── The privacy gate: EVERY surface a commit publishes ──────────────────────────────────────────
# Emitted by `luxarch --emit gitleaks`. Drop in verbatim.
#
# `gitleaks` scans DIFF CONTENT. A commit's author/committer address lives in the commit object
# HEADER and never appears in a patch, so no content rule can ever match it — it is a surface the
# scanner does not read. A repo reported `no leaks found` over 963 commits while 29 of them carried a
# personal address in both the author and committer fields, and it would have reported exactly the
# same thing after the scrub: identical output, opposite truth. Measured across the fleet, EIGHT
# repos carry a personal address in history and two of them are PUBLIC.
#
# v13: the commit MESSAGE is a third surface, and through v12 nothing read it. It is neither a patch
# nor the header, so a message naming an org-denylisted host passed the staged scan, the full-history
# scan and the push hook: seven commits in one public repo, found only when a file repeated the name.
# Every commit and annotated-tag message in history is now scanned with the same config as file
# content, and `gitleaks-commit-msg` scans the message being written (run by hooks/commit-msg).

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

# v13: `/repo` is `$(CURDIR)`, this makefile's directory. v1-v12 mounted `$(PWD)`, the CALLER's shell
# directory, so `make -C <repo> gitleaks` run from another repo scanned THAT repo's history with THAT
# repo's config while the identity pass read this one: two repos' answers, reported as this one's.
gitleaks: ## secret scan over FULL HISTORY + the commit-identity pass (the hooks cover commit/push)
	@set -e; C=$$(mktemp); trap 'rm -f "$$C"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	docker run --rm -v "$(CURDIR)":/repo -v "$$C":/gl.toml:ro -w /repo \
	  $(GITLEAKS_IMAGE) git /repo -c /gl.toml --redact -v --ignore-gitleaks-allow
	@# v12: what `gitleaks git` never reads. It scans git's PATCHES, and git prints no patch for a file it
	@# treats as binary: a NUL byte, UTF-16 (a `Localizable.strings`), or a `binary` / `-diff` attribute.
	@# A token in any of them reached the remote with "no leaks found". So every path git ever showed as
	@# binary is re-read as text, NULs stripped, and scanned by path (the config's path allowlists hold).
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
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
	@# v13: every commit and annotated-tag MESSAGE, scanned as a file with the same config (credential
	@# rules and the denylist). One `git log` and one `for-each-ref`, split on a header line carrying a
	@# per-run nonce, so a message cannot forge a header and steer its own lines out of the scan.
	@set -e; C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	N=$$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n'); [ $${#N} -eq 32 ]; \
	mkdir -p "$$D/t/commit-message" "$$D/t/tag-message" "$$D/none"; \
	git log --branches --tags HEAD --format="$$N commit-message/%H%n%B" > "$$D/raw"; \
	git for-each-ref refs/tags --format="$$N tag-message/%(objectname)%0a%(contents)" >> "$$D/raw"; \
	tr -d '\000' < "$$D/raw" > "$$D/msgs"; \
	LC_ALL=C awk -v n="$$N" -v d="$$D/t" \
	  '$$1 == n && NF == 2 { if (f != "") close(f); f = d "/" $$2; printf "" > f; next } \
	   f != "" { print > f }' "$$D/msgs"; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none || { \
	  echo "REFUSING: a commit or tag MESSAGE carries a finding (the File above names it:"; \
	  echo "  commit-message/<sha> or tag-message/<object>). A message is published with its commit"; \
	  echo "  forever. Reword it (git rebase -i, or git filter-repo --message-callback for older"; \
	  echo "  history), re-run, and ask the OWNER to approve the force-push. Never force-push unapproved."; \
	  exit 1; }
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
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
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

# v13: the message being WRITTEN, run by hooks/commit-msg (pre-commit cannot see it: git writes the
# message after that hook runs). The hook passes the file in `GITLEAKS_COMMIT_MSG`. The WHOLE file is
# scanned, comment lines included: `--cleanup=verbatim` publishes them, and a cut at the scissors line
# would let a verbatim message hide everything below one. No file to read is a refusal, never a pass.
gitleaks-commit-msg: ## secret + denylist scan of the commit MESSAGE being written (run by hooks/commit-msg)
	@set -e; M="$${GITLEAKS_COMMIT_MSG:-}"; \
	if [ -z "$$M" ] || [ ! -f "$$M" ] || [ ! -r "$$M" ]; then \
	  echo "REFUSING: no commit message to scan (GITLEAKS_COMMIT_MSG='$$M'). hooks/commit-msg" >&2; \
	  echo "  passes the file git hands it; re-emit the hooks with luxlint --emit-hooks." >&2; \
	  exit 1; \
	fi; \
	C=$$(mktemp); D=$$(mktemp -d); trap 'rm -rf "$$C" "$$D"' EXIT INT TERM; \
	docker run --rm -v "$(CURDIR)":/repo $(LUXLINT) --emit-config gitleaks > "$$C"; \
	mkdir -p "$$D/t/commit-message" "$$D/none"; \
	tr -d '\000' < "$$M" > "$$D/t/commit-message/COMMIT_EDITMSG"; \
	docker run --rm -v "$$D/t":/scan:ro -w /scan -v "$$D/none":/none:ro -v "$$C":/gl.toml:ro \
	  $(GITLEAKS_IMAGE) dir . -c /gl.toml --redact -v --ignore-gitleaks-allow \
	  --gitleaks-ignore-path /none || { \
	  echo "REFUSING: this commit's MESSAGE carries a finding. A message is published with the" >&2; \
	  echo "  commit forever: reword it and commit again. Never --no-verify." >&2; \
	  exit 1; }

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

# luxarch:test-block asset v2 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit test-block`.
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
#     warnings are errors), mounted read-only. The source is mounted read-only and pytest writes no
#     cache into it.
#   - THE RUN'S FILES stay private (v2): the run's directory keeps mktemp's 700, so no other local
#     user can reach the config pytest loads (a config can load plugins, in a container holding the
#     repo and the suite's credentials). Inside it, `cfg/` is 711 and `out/` is 733: the image's
#     non-root user, whatever its uid, can open the config by name and write its coverage data, and
#     nothing more. Measured: the container reaches a bind mount without the host path above it, and
#     a narrower `out/` (711, 730, 755) fails coverage's write. v1 made the whole directory 777.
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
# A committed value narrows THE suite for everyone (`repo.test_block_wired` reds one). The ratchet
# still judges the whole floor, so a narrowed run measures only what it touched and fails it: a
# quick subset target of your own runs pytest without the ratchet, never through `make test` ----
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
	trap '$$dc down --volumes --remove-orphans >/dev/null 2>&1; rm -rf "$$D"' EXIT INT TERM; \
	mkdir "$$D/cfg" "$$D/out" && chmod 711 "$$D/cfg" && chmod 733 "$$D/out" || exit 2; \
	net=; if [ -n "$(TEST_SERVICES)" ]; then \
	  $$dc up -d --wait --wait-timeout 120 $(TEST_SERVICES) \
	    || { echo "FAIL  the test services did not become healthy: $(TEST_SERVICES)"; exit 1; }; \
	  cid=$$($$dc ps -q $(firstword $(TEST_SERVICES))); net=; \
	  for n in $$(docker inspect -f '{{range $$k, $$v := .NetworkSettings.Networks}}{{$$k}} {{end}}' $$cid); do \
	    [ "$$(docker network inspect -f '{{index .Labels "com.docker.compose.project"}}' $$n)" = "$$run" ] && { net=$$n; break; }; done; \
	  [ -n "$$net" ] || { echo "FAIL  $(firstword $(TEST_SERVICES)) joined no network of this run's own project ($$run)"; exit 1; }; \
	  net="--network $$net"; fi; \
	docker run --rm -v $(CURDIR):/repo $(LUXLINT) --emit-config pytest > "$$D/cfg/pytest.ini" || exit 2; \
	chmod 644 "$$D/cfg/pytest.ini"; \
	docker run --rm --init $$net $(TEST_ENV) -e PYTEST_ADDOPTS="$${PYTEST_ARGS:-}" \
	  -e COVERAGE_CORE=sysmon -e COVERAGE_FILE=/out/.coverage -e PYTHONDONTWRITEBYTECODE=1 \
	  -v $(CURDIR):/repo:ro -v "$$D/cfg":/cfg:ro -v "$$D/out":/out -w /repo/$(TEST_WORKDIR) $(TEST_IMAGE) \
	  sh -c 'python -m coverage run --branch --source=$(TEST_COV) -m pytest -c /cfg/pytest.ini --rootdir=. -p no:cacheprovider; s=$$?; python -m coverage report --show-missing > /out/coverage.txt; echo $$? > /out/coverage.rc; cat /out/coverage.txt; exit $$s'; \
	rc=$$?; \
	[ "$$rc" = 0 ] || { echo "FAIL  the suite failed (exit $$rc)"; exit 1; }; \
	[ "$$(cat "$$D/out/coverage.rc" 2>/dev/null)" = 0 ] || { echo "FAIL  coverage measured nothing (coverage report: $$(tail -n 1 "$$D/out/coverage.txt" 2>/dev/null)): check TEST_COV names the package the suite imports"; exit 1; }; \
	docker run --rm -i -v $(CURDIR):/repo $(LUXLINT) --coverage-ratchet < "$$D/out/coverage.txt" > "$$D/ratchet.txt"; crc=$$?; \
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
