# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

<!-- Maintainer-only working notes (roadmap, internal tooling, operational
     snapshots) live outside the repo and are imported below. The import is a
     no-op for anyone who doesn't have the file (e.g. public clones). -->

@~/.claude/projects/luxswirl.md

<!-- luxarch:claude-pointer asset v12 - DO NOT edit this marker line; it is how repo.claude_pointer_present knows your copy is current. Re-emit with `luxarch --emit claude-pointer`. -->

## How to work here (fleet conduct — read the standard, not just this block)

**`luxarch --doc FLEET-AGENT-CONDUCT-STANDARD` — read it in full before your first change.** It is the one home for *how* agents work in this fleet. This block is a pointer plus the handful of rules that get broken most; it is not a summary and does not replace reading it.

**`luxarch …` is a Docker image, not a host command.** Run `luxarch --doc X` as `docker run --rm -v <checkout>:/repo -w /repo <image> --doc X`, where `<checkout>` is the absolute path of the checkout you are in, written out (your issue's worktree when you are in one: inside a worktree Claude Code refuses a mount computed at run time, such as `"$PWD"`), and `<image>` is the Makefile's pinned `LUXARCH` image (set from `LUXARCH_VERSION`). There is no host install, and the pinned version is the one this repo's rules and docs match. `luxlint …` and `luxaudit …` are the same, with `LUXLINT` and `LUXAUDIT`.

**Which agent are you?** In a repo that hosts a luxios app (a nested `apps/ios/` with its own `CLAUDE.md`, beside the backend), there are two agents. If this machine has no Docker (`command -v docker` prints nothing), you are that app's agent: read its `CLAUDE.md` first, follow the iOS reading below, and leave the root guard gates (`make check`, the luxarch, luxlint and luxaudit images) to the backend agent. Never look for a Docker binary to install.

**Run `/fleet-start` at the start of every session and after every compact.** It rehydrates from LuxPM and restates the session rules.

**Report the result, not the mountain.** No "heavy", "multi-hour", "the big one", no narrating difficulty. Done + next in one line, with numbers.

**Decide; do not hand back a menu.** Whether to ask the owner is decided by the **class of action**, never by how confident you feel:

- **Ask** — deleting anything; changing scope; a deferral/allowlist/exemption; publishing outward (pushing another repo, a force-push, a history rewrite); a genuine product fork where the choice is taste, not correctness.
- **Do it** — aligning code to a ratified standard or a guard red; anything you have evidence for that is reversible in one commit. The standard already decided; say what you did.

**Work the guard reds in `luxarch --plan` order. Never ask which family or sweep is next.** The order is decided. An escalation covers ONE site: its family keeps going.

**Check one rule or one file with a targeted run, never a filtered full scan.** `luxarch --rule <id>` runs only the matching rules (exact or substring, repeatable); add `--path <file>` to show one file's sites. The whole repo's per-rule counts are in `.luxarch-status.json` (`make status`). A full scan piped into `grep`, `head` or `tail` is denied by the emitted `targeted-guard-run` hook.

**An owner hold is exactly as wide as the owner said.** "Hold off on X" excludes X and nothing else. It is not permission to pause, ask, or check in about anything outside X. Skip the held family, say so in one line, and keep burning down the rest.

**End every turn that worked guard reds with `reds: N (was M)`**, plus the held families by name. A turn that ends on a question while N > 0, outside an ask-class, is the failure this block exists to stop.

When you do ask: **one decision per message**, the evidence that makes it answerable, your recommendation stated as one, and a question answerable in one word. **A recommendation that ends in a menu is not a recommendation** — if you rejected the alternatives, re-offering them asks the owner to redo your analysis.

**Use the fleet skills; don't improvise the procedure.** `/fleet-start` to open a session. `/wrap-up` before you call anything done (tests you saw fail before the fix, every red in touched files, docs, gate, LuxPM closed out, all with evidence). `/adversarial` to have an independent agent attack a change touching auth, tenancy, data, money, secrets or deploys. `/pin-bump` to upgrade the guards. `/escalate` when a guard is wrong. `/release` to cut a release.

**You touched it, you own it.** Edit a file for any reason and it has a mypy, ruff or luxarch red: fix every one in that file, not just yours. Never spend time proving a red predates you; fix it. Test what you changed first. **Before fixing any mypy red, read `luxlint --playbook mypy-sweep` in full.**

**Align or escalate; never route around.** A guard red is fixed by changing the code, or escalated to the guard maintainer as genuinely wrong. Never by an exemption, a `# noqa`, a deferral, or a local config. Verification is not authorization: proving something is unreferenced does not license deleting it.

**Search LuxPM for a file before you change it to clear a red.** `luxpm_list_issues(search="<path>")`, every hit read, then `git log -- <file>`. A site that is on hold, in another agent's work, or under an open escalation is skipped and named, never changed. A red moved to another file, or out of a rule's sight, is not fixed: `/wrap-up` compares the whole repo's per-rule counts.

**Escalations go in THIS repo's LuxPM project** — label `fleet-escalation`, title `[<guard> ESCALATION] …`, self-contained enough to forward whole. **Search LuxPM for an existing issue first** (and comment on it if found); filing a new one is pre-authorized. **Never a GitHub issue** — there is no fallback. The maintainer sweeps the label across every project and picks it up where you filed it.

**A red stays RED while its escalation is open.** The fleet does not gate CI on red. A lit red is honest; a silenced one is a lie you will inherit.

**Commit as `luxardolabs`** using the global git config, and never `git -c user.email=…`. No AI attribution in commit messages.

## Project Overview

**LuxSwirl** is a production-quality SaaS observability monitoring platform for all types of metrics developed by Luxardo Labs. It consists of three main components:

- **Agent**: Executes health checks (ping, HTTP, TCP, JSON) and reports results to the server
- **Server**: FastAPI server (agent API + web UI) with production-grade architecture (models, schemas, services, routers)
- **TimescaleDB**: PostgreSQL extension providing time-series storage with automatic partitioning and retention

The system is designed as a professional SaaS platform with:

- Complete separation of concerns (models, schemas, services, routers)
- Async/await patterns throughout for high performance
- Pydantic Settings for type-safe configuration
- SQLAlchemy 2.0 with async support
- TimescaleDB hypertables for efficient time-series storage
- A versioned agent protocol under `/api/v1/agent`
- Proper error handling and validation
- Prometheus metrics export

## Architecture

**Canonical reference:** `docs/architecture/backend.md` — read it before adding code.

### Repository layout

```
apps/                                # one self-contained component per dir (luxwx pattern)
├── backend/                         # Server + portal (FastAPI + HTMX) — its own image
│   ├── app/                         # the `app` package (app.* imports)
│   │   ├── api/v1/routers/          # JSON endpoints
│   │   ├── web/routers/             # HTMX + Jinja2 pages
│   │   ├── services/core/           # Business logic, transactions
│   │   ├── services/views/          # Template context assembly
│   │   ├── crud/                    # The ONLY place that runs raw SQL
│   │   ├── models/                  # SQLAlchemy ORM
│   │   ├── schemas/                 # Pydantic request/response
│   │   ├── core/                    # Config, security, exceptions, utils
│   │   ├── db/                      # Async session
│   │   └── main.py                  # FastAPI entrypoint (uvicorn app.main:app)
│   ├── alembic/ + alembic.ini       # DB migrations (component root, baked into image)
│   ├── tests/                       # Backend test suite
│   ├── pyproject.toml + poetry.lock # Backend deps only (no agent/browser deps)
│   ├── Dockerfile + Dockerfile.lint # Runtime image + throwaway lint image
│   └── package.json + tailwind.config.js
├── agent/                           # Health-check agent — its own (separate) image
│   ├── app/
│   │   ├── agent/core.py            # LuxSwirlAgent class
│   │   ├── checks/                  # ping, http, tcp, json, dns, mysql, postgres, synthetic
│   │   ├── health/check.py          # Docker HEALTHCHECK (python -m app.health.check)
│   │   └── main.py                 # Agent entrypoint (python -m app.main)
│   ├── tests/
│   ├── pyproject.toml + poetry.lock # Agent deps only (playwright, db drivers, …)
│   └── Dockerfile + Dockerfile.lint
└── shared/                          # config, logger, url_security, subprocess_safe, jobs/
                                     # — the SSOT, overlaid on each leg's placeholder app/shared

VERSION                              # version source of truth (NOT derived from path)
Makefile                             # build/dev/prod/lint/test — everything runs in docker
compose*.yaml                        # base + dev/prod/agent/test overlays
```

Each component is its own deployable: `apps/backend/app/` and `apps/agent/app/` are both the **`app` package**, so imports are **`app.`-prefixed** (`from app.models.foo import ...`). Shared code is imported as **`app.shared.*`**: each leg carries the 0-byte placeholder package `app/shared/` (its `__init__.py`), and the real `apps/shared` (the `[shared]` SSOT in `.luxarch.toml` and `.luxlint.toml`) is overlaid on it, baked by both images (`COPY apps/shared /app/app/shared`) and mounted by `make lint`'s mypy run and `make test` (the Makefile's `shared_overlay`). Modules inside `apps/shared` import each other relatively. Because both components use the name `app`, they have separate pyprojects/pytest configs and **cannot** share one test run. One Dockerfile per component.

### Layering chain (enforced)

```
JSON API:  api/v1/routers/  →  services/core/  →  crud/  →  models/
Web UI:    web/routers/     →  services/views/ →  services/core/  →  crud/  →  models/
```

- **Routers** do HTTP plumbing only: zero business logic, zero raw SQL, zero view-context assembly.
- **Web routers** call view services only — never `services.core` or `crud` directly.
- **API routers** call core services only — never `services.views` or `crud` directly.
- **View services** assemble template context (pagination math, multi-service aggregation). One view service does not import another (except `_*.py` helpers).
- **Core services** own business logic and transactions. No raw SQL.
- **CRUD** is the only layer that runs `db.execute(...)`, `select()`, `update()`, `delete()`, `text()`.

**Naming (also enforced):** `*_router.py`, `*_view_service.py`, `*_core_service.py`, `*_crud.py`, `*_model.py`, `*_schema.py`. Files prefixed with `_` are shared helpers exempt from the naming rule (use only for genuinely multi-consumer helpers).

**Enforcement:** `make arch` runs the luxarch conformance guard (architecture rules + this repo's facts/levels in `.luxarch.toml`: raw SQL, naming, LOC limits, business logic in routers, …) plus `pyproject.toml [tool.importlinter]` (6 import-graph contracts, run as the in-repo tail). The former in-repo grep guards were migrated into luxarch; `make lint`/`make test` need no extra setup, and `make arch` skips luxarch gracefully if its registry isn't configured (see `Makefile.local.example`).

### Three-component runtime

1. **luxswirl_agent** (`apps/agent/`) — Runs health checks. Entry: `apps/agent/app/main.py` (`python -m app.main`). Pushes results to server via HTTP. Built as its own image.
1. **luxswirl_server** (`apps/backend/`) — FastAPI app: agent protocol at `/api/v1/agent`, web dashboard at `/dashboard`, Prometheus at `/metrics`. Entry: `apps/backend/app/main.py` (`uvicorn app.main:app`). Built as its own image.
1. **TimescaleDB** — PostgreSQL extension. Automatic partitioning, configurable retention, continuous aggregates. Schema lives in `apps/backend/app/models/*_model.py`; DDL helpers live in `apps/backend/app/crud/timescale_crud.py`.

### Web UI

**Stack:** HTMX (10-second polling) + Tailwind CSS 3.4 (dark-mode theme) + Chart.js + Jinja2.

**Layout:**

```
apps/backend/app/
├── web/routers/                     # HTMX/Jinja route handlers (HTTP plumbing only)
├── templates/web/
│   ├── pages/                       # Full pages
│   ├── partials/                    # HTMX swap targets
│   └── macros/                      # Reusable Jinja components
├── static/                          # css/input.css -> css/app.css (Tailwind, built in the image; gitignored), JS, images
└── services/views/                  # Template context assembly
    ├── *_view_service.py            # One per page family
    └── _dashboard_render.py         # Shared helper (underscore-prefixed)
```

Routers stay tiny: parse query params, call `await SomeViewService.build_*_context(...)`, hand the dict to `templates.TemplateResponse`. Pagination math, multi-service aggregation, default-from-settings fallback all live in the view service.

**Features:** real-time updates (HTMX 10s polling), side-panel check details, 4h/8h/24h/3d/7d range selector, 30-minute status bars, Chart.js latency charts, URL-state filtering, configurable pagination (10–200), dark mode, responsive.

### Check System

All checks inherit from `BaseCheck` (in `apps/agent/app/checks/base.py`) which provides:

- Configuration validation
- Timer utilities for latency measurement
- Standardized result format creation

Available check types (in `apps/agent/app/checks/`):

- `ping.py` - ICMP ping checks with cross-platform command handling
- `http.py` - HTTP/HTTPS endpoint checks
- `tcp.py` - TCP port connectivity checks
- `json.py` - JSON API validation checks using JSONata query language
- `dns.py` - DNS query checks (A, AAAA, MX, TXT, etc.)
- `mysql.py` - MySQL/MariaDB database query checks
- `postgres.py` - PostgreSQL database query checks
- `synthetic.py` - Playwright browser automation checks (**⚠️ Admin only - executes arbitrary code**)

**Synthetic Check Security (IMPORTANT):**

- **Admin Role Required**: Only admins create, change, move, enable/disable or delete a synthetic check, by any path (single, bulk, dependents, import, admin API), enforced in the core services (`CheckCoreService`, `CheckDependencyCoreService`), not just the web layer. An agent change that alters where one runs (a rename or retag that moves the agent into or out of a synthetic check's selector, approving an agent one already matches) or deletes one (deleting an agent that owns it) is an admin's too (`AgentCoreService`); other agent edits stay with editors.
- **Arbitrary Code Execution**: Runs user-provided Python scripts using `exec()` with Playwright
- **AST Validation**: Scripts validated to block obvious attacks (eval, exec, os, subprocess, etc.)
- **Security Audit Logging**: All operations logged with `SECURITY AUDIT` prefix
- **UI Warning Banner**: Prominent security warning displayed when creating/editing
- **Trusted Environments Only**: Designed for self-hosted, single-organization deployments
- **NOT Multi-Tenant Safe**: v1.0 requires Kubernetes pod isolation for managed SaaS
- See `SECURITY.md` for complete security model and threat analysis

**JSON Check Details:**

- Uses JSONata (jsonata.org) for querying JSON responses
- Full Uptime Kuma compatibility - queries can be copied directly from Uptime Kuma
- Supports advanced queries:
  - Simple paths: `data.users.name`
  - Array indexing: `data.users[0].name`
  - Quoted keys (for keys with dots): `printers."printer.with.dots".status`
  - Wildcards: `printers.*.status`
  - Filters: `printers[status="online"]`
  - Functions: `$count(printers)`, `$sum(printers.*.page_count)`
  - Predicates: `printers[page_count > 1000]`
- See https://jsonata.org for full query syntax
- Playground available at: https://try.jsonata.org

To add a new check type:

1. Create a new file in `apps/agent/app/checks/` that inherits from `BaseCheck`
1. Implement the `run()` method returning a result dictionary
1. Register it in `apps/agent/app/main.py` using `agent.register_check_type()`

### Configuration System

Configuration is handled by `apps/shared/config.py` (imported as `app.shared.config`):

- Default configurations are embedded in the code (see `DEFAULT_AGENT_CONFIG` and `DEFAULT_SERVER_CONFIG`)
- Environment variables override defaults (e.g., `LUXSWIRL_AGENT_ID`, `LUXSWIRL_AUTH_KEY`, `LUXSWIRL_SERVER_URL`)
- Configuration can also be loaded from YAML/JSON files via `--config` flag
- The `get_config(type)` function returns config for "agent" or "server"

### Reporter System

The agent uses a sophisticated batching reporter (`apps/agent/app/agent/reporter.py`):

- Results are batched before sending to reduce HTTP overhead
- Configurable batch size (`report_batch_size`, default 5000) and time interval (`report_interval`, default 10s)
- Automatic retries with exponential backoff
- **SQLite storage for failed reports** — single `reports/pending_reports.db` file with gzip-compressed payloads
  - Replaces legacy per-file JSON storage (which caused inode exhaustion during prolonged outages)
  - WAL journal mode for concurrent read/write safety
  - Automatic migration of legacy `report_*.json` files on first startup
  - Disk cap: `report_max_stored_batches` (default 10,000) prunes oldest batches when exceeded
  - Uses `asyncio.to_thread()` for non-blocking I/O (stdlib `sqlite3`, zero new dependencies)
- Background task processes stored reports when server becomes available, combined up to `report_batch_size` per POST
- **Per-result limits** (`apps/agent/app/agent/result_limits.py`): before a result is queued, an over-long `error`/`response_data` is truncated and an `http_status_code` outside 100-599 is sent as null (the code goes to `metrics.http_status_code_raw`). The server validates each result on its own, so one it cannot store is counted in `results_failed` and the rest of the report is kept
- **Dead letters for permanent refusals** (`apps/agent/app/agent/dead_letter.py`)
  - Only the server's own word is permanent: a 400 or 422 whose body is the app's JSON error envelope with `error` = `VALIDATION_ERROR` or `BAD_REQUEST`. That batch is not retried or stored, it goes to the `dead_letters` table in the same database (original `created_at`, `failed_at`, status, the server's error detail scrubbed by `shared/log_redaction.py` and cut to 2 KB, and the gzip payload), with an ERROR log line
  - Everything else is transient and stored for replay: network errors, 5xx, 408, 429, 401/403 (key recovery can fix auth), and any 4xx without the app's envelope, such as the trusted-host middleware's plain-text `400 Invalid host header` or a proxy's own 400. A misaddressed agent keeps its queue
  - A 413 (a proxy's body limit; the app never sends 413) splits the batch: it is sent in halves, each halved again while still refused. A single result the limit still refuses stays pending, never dead-lettered, and the split goes on past it; two refused with nothing sent stop it. What was not sent is stored as one row, refused results last (replay rewrites its row in place), and replay reads past a row the limit holds until nothing else is left
  - Replay never stops at a refused batch: a combined POST refused permanently is retried batch by batch; each one the server takes is deleted, each one it refuses moves to `dead_letters`, and a transient failure ends the pass
  - Cap: `report_max_dead_letters` (default 1,000) prunes the oldest
  - The count rides each heartbeat as `dead_letter_count`, is stored on the agent row, and shows as a yellow "N dead-lettered" badge on the agent's card when above zero
- Backpressure handling: drops oldest results when queue exceeds `report_max_queue_size`

### Agent Performance Features

The agent includes several reliability features in `agent/core.py`:

- **Concurrency control**: Semaphore limits concurrent checks (`max_concurrent_checks`, default 200)
- **Self-monitoring**: Agent reports its own health metrics periodically
- **Watchdog**: Monitors result processing and forces flush if stalled
- **Graceful shutdown**: Handles SIGTERM/SIGINT, processes remaining results before exit
- **Per-check intervals**: Each check can have its own interval, independent of global setting

## Common Commands

**Everything runs in Docker via the Makefile** — the only host requirements are `make` + `docker` (no host Python, poetry, or node). `make help` lists all targets, grouped. Compose **never builds**: the Makefile builds the baked images, compose runs them.

### Dev stack

`make dev-deploy` is the deploy: it publishes this commit (both images built, scanned and pushed to the private `REGISTRY` as `:sha-<commit>`), pins `.env.dev` to it, brings the stack up and waits for every HEALTHCHECK, then runs `make smoke` against what it deployed. A smoke FAIL fails the deploy. It refuses a dirty tree: commit first. The steps underneath stay separate, and `dev-up` does NOT build.

```bash
make dev-deploy    # publish-sha + dev-pin + smoke: the way to put a commit on the dev stack
make smoke         # probe the deployed stack: /health names the commit, CSS served, forged Host refused, auth, containers healthy, entry points import
make publish-sha   # PUBLISH ONLY: build + scan + push both images as :sha-<commit> (an already-published sha is reused, never rebuilt)
make dev-up        # START ONLY: `compose up -d` — does NOT build; runs whatever tag .env.dev pins
make dev-pin TAG=sha-…  # point .env.dev at an already-PUBLISHED tag and restart (the rollback path)
make dev-restart   # restart the running containers ONLY — no rebuild, does NOT pick up code changes
make dev-logs      # follow logs
make dev-shell     # bash into the server container
make dev-down      # stop the stack (volume preserved)
```

Dev runs **baked images** (code COPYd in — no source bind-mounts), so a code change reaches the stack only through a build: use `make dev-deploy`. `make dev-up` alone recreates the containers onto the tag `.env.dev` already pins, and `make dev-restart` just bounces them; both run the old code. `make smoke` needs `SMOKE_URL` in `Makefile.local` (see `Makefile.local.example`); its authenticated probe runs when `Makefile.local` sets `SMOKE_AUTH_PATH` (e.g. `/metrics` on a stack that requires the metrics token) and `SMOKE_AUTH_TOKEN` is in the environment. The `luxswirl_db_data` volume persists across `down`/`up`; only `make clean-all` (`down -v`) drops it.

### Build & release

```bash
make version                # show VERSION + image tags
make build                  # build both images LOCALLY as bare luxswirl-{backend,agent}:$(VERSION) (no push)
make release                # cut VERSION: refuse a dirty tree / released version / untagged HEAD, build + scan + push both :$(VERSION) to ghcr, then the GitHub Release
```

Publishing is the luxarch **image block** (v16, `IMAGES = backend agent`): one `VERSION`, both images, every image scanned before any is pushed. Version is **coded in the `VERSION` file** (not derived from the directory path) → baked via `--build-arg BUILD_VERSION` → `ENV APP_VERSION`/`LABEL version` → `settings.app_version` → `/health`. Registries: each commit's `:sha-<commit>` goes to the private `REGISTRY` (from `Makefile.local`); a release goes to `RELEASE_REGISTRY` = `ghcr.io/luxardolabs/luxswirl-{backend,agent}`. The backend Dockerfile's `test` stage (production + the dev group) is the suite's image.

### Quality & tests (all dockerized)

```bash
make lint          # ruff check + format-check + mypy for BOTH components (fresh Dockerfile.lint images)
make test          # backend suite: fresh test image + its own TimescaleDB per run, coverage ratchet
make arch          # architecture conformance (luxarch guard + import-linter contracts)
make gitleaks      # secret scan (pre-push hook runs it on the pushed commits)
make compileall    # fast syntax check (byte-compile) in docker
make check         # lint + test (boutique-style pre-commit suite)
make poetry-lock   # regenerate poetry.lock for BOTH components (docker; no host poetry)
```

See `docs/architecture/backend.md` for the full layering rules and naming conventions.

## Key Configuration Parameters

### Agent Performance Tuning

- `max_concurrent_checks`: Maximum checks running simultaneously (default: 200)
- `report_batch_size`: Results per batch sent to server (default: 5000). Caps both fresh in-memory batches and combined SQLite-replay POSTs.
- `report_interval`: Seconds between batch sends (default: 10)
- `report_max_queue_size`: Max results in memory before dropping (default: 5000)
- `interval`: Default check interval if not specified per-check (default: 60)

### Server Settings

- `port`: Server listen port (default: 9000)
- `metrics_ttl_seconds`: How long to keep check results (default: 300)
- `max_history_points`: Historical data points per check (default: 1000)

## API Endpoints

The JSON API has two surfaces, each with its own prefix and credential: the agent protocol (`/api/v1/agent`) and the admin API (`/api/v1/admin`). Operators manage agents, checks, alerts, notification providers, registration keys, users and jobs through the web UI.

### Agent protocol (`/api/v1/agent`)

| Method | Path                                   | Auth                               | Purpose                                  |
| ------ | -------------------------------------- | ---------------------------------- | ---------------------------------------- |
| `POST` | `/api/v1/agent/register`               | Registration key                   | Register a new agent (pending approval)  |
| `POST` | `/api/v1/agent/{agent_id}/recover-key` | Registration key + recovery secret | Issue a fresh agent key to a known agent |
| `POST` | `/api/v1/agent/heartbeat`              | Agent key or registration key      | Report liveness and self-metrics         |
| `GET`  | `/api/v1/agent/checks?agent_id={id}`   | Agent key                          | The checks this agent should run         |
| `POST` | `/api/v1/agent/reports`                | Agent key                          | Submit a batch of check results          |
| `POST` | `/api/v1/agent/jobs/{job_id}/start`    | Key of the job's own agent         | Report a job started (it is running)     |
| `POST` | `/api/v1/agent/jobs/{job_id}/results`  | Key of the job's own agent         | Submit a job's results                   |
| `POST` | `/api/v1/agent/artifacts`              | Agent key                          | Upload an artifact for one of its checks |

The agent builds every one of these from `LUXSWIRL_SERVER_URL`, the server's address with no path (`apps/agent/app/agent/server_api.py`), and refuses to start on one that carries a path, credentials or an invalid port.

### Admin API (`/api/v1/admin`)

Authenticated by a user's API token (`Authorization: Bearer lsw_...`, created in the My Account panel), which acts as that user with their role: `ApiUser` / `ApiEditor` / `ApiAdmin` in `app/api/deps.py`, rate-limited per token. See `docs/user-guide/admin-api.md`.

| Method | Path                         | Role | Purpose                                     |
| ------ | ---------------------------- | ---- | ------------------------------------------- |
| `GET`  | `/api/v1/admin/me`           | any  | The token's user, their role, and the token |
| `GET`  | `/api/v1/admin/openapi.json` | any  | The admin API's OpenAPI document            |

Checks (`/api/v1/admin/checks`, by id): `GET` list (the `PaginatedResponse` page envelope; filters agent_id, check_type, tag, search, enabled_only) and `GET /{id}` for any token; `POST`, `PATCH /{id}`, `DELETE /{id}`, `POST /{id}/clone`, `/enable`, `/disable` for an editor, and for an admin alone on a synthetic check. A viewer's token sees every credential in `target` and `connection_string` masked and no `script_code` (`AdminCheckCoreService._response`, through `core/credential_mask.py`).

Bulk (`/api/v1/admin/checks/bulk/{enable,disable,delete,modify}`, editor, 1 to 10,000 `check_ids`): enable and disable run now, while delete and modify queue a maintenance job and answer `202` with it. The whole selection is judged first (`AdminCheckCoreService._judge_selection`): an unknown id is a 422, and a synthetic check in a non-admin's selection is a 403, with nothing changed or queued. The bulk job handlers (`maintenance_handlers/_bulk_outcome.py`) refuse a job whose owner is no longer an active editor or admin, and keep their counts and first errors in the job's `progress`, on a failed job too (`JobFailed`, `maintenance_worker.py`); a failed job's `error` is stored without SQL or credentials (`MaintenanceJobCoreService.mark_failed`). `GET /api/v1/admin/maintenance-jobs/{id}` (any token) returns the job. Dependents: `GET`/`PUT /api/v1/admin/checks/{id}/dependents` (PUT replaces the whole set, editor). Results (any token, `AdminCheckResultCoreService`): `GET /api/v1/admin/checks/{id}/results` (paged, `since`/`until` window, last 24 h by default, at most 31 days), `/results/{result_id}` (with `response_data` and artifact metadata; only through its own check), `/history` and `/summary` (`hours` 1-720). A viewer reads every `error` masked, no `response_data`, and of `metrics` only the detail panel's sections (dns, mysql, postgres, synthetic, `response.ssl_certificate`), masked. List `page` is capped at `MAX_PAGE` (`schemas/admin_page_schema.py`).

Agents (`/api/v1/admin/agents`, by id; `AdminAgentCoreService`): `GET` list and `GET /{id}` for any token; `PATCH /{id}` (the web edit form's fields; agent-reported `agent_run_id`/`ip_address`/`version` refused), `DELETE /{id}` (202, `agent_delete` job), `POST /{id}/{approve,reject,pause,resume,disable,enable,archive,restore,force-reload}`, `POST /{id}/key`, `POST /{id}/key/regenerate`, `DELETE /{id}/key` for an editor, with the synthetic-placement rules of `AgentCoreService`. Approve returns no key. Alerts (`/api/v1/admin/alerts`; `AdminAlertCoreService`): list and get (with `notification_provider_ids`, `check_ids`) for any token; create, `PATCH`, soft `DELETE`, enable/disable, `PUT /{id}/providers` and `PUT /{id}/checks` (replace by difference, `AlertCRUD.replace_*_mappings`, kept rows keep their state and snooze) for an editor; `POST`/`DELETE /{id}/checks/{check_id}/snooze` for any token. Unknown provider or check ids are a 422, checked before any write; `trigger_config` is validated against its trigger type (`schemas/alert_trigger_config_schema.py`); attaching or detaching a synthetic check is an admin's; `is_global` is fixed at creation; a snooze ends within a day (`AlertCoreService.MAX_SNOOZE`, the web's too). Registration keys (`/api/v1/admin/registration-keys`, admin only; `AdminRegistrationKeyCoreService`): list, get, create (`201`, plaintext `key` once), `POST /{id}/revoke` (a revoked key is refused again with 422); `created_by`/`revoked_by` are the token's username. Every destructive maintenance job handler re-judges its owner as an active editor or admin when it runs (`maintenance_handlers/_job_owner.py`).

### Infrastructure

- `GET /metrics` - Prometheus exposition (optional bearer token from Settings > API Keys)
- `GET /health` - Server health check (status, version, commit)
- `GET /api` - API information
- `GET /docs`, `GET /redoc` - OpenAPI documentation (outside production)

### Prometheus Metrics

The `/metrics` endpoint exposes:

- `luxswirl_check_success` - Whether check succeeded (1/0)
- `luxswirl_check_up` - Whether check is still reporting (1/0)
- `luxswirl_check_latency_seconds` - Check latency in seconds
- `luxswirl_check_last_execution_time` - Timestamp of last execution
- `luxswirl_agent_up` - Whether agent is reporting (1/0)

All metrics include labels: `agent`, `check`, `type`, `target` (the target with any credential masked)

## Security

### Agent-Server Communication

**HTTPS Enforcement:** External servers MUST use HTTPS to protect credentials in transit. Agent credentials are transmitted in the `Authorization` header and must be encrypted.

**Allowed without HTTPS (internal/local networks):**

- Same Docker network: `http://server:9000`, `http://luxswirl_server:9000`
- Localhost: `http://localhost:9000`, `http://127.0.0.1:9000`
- Private networks (RFC 1918): `http://192.168.1.x:9000`, `http://10.x.x.x:9000`, `http://172.16-31.x.x:9000`
- Local domains: `http://hostname.local:9000`

**Requires HTTPS (external/public):**

- External hostnames: `https://server.example.com:9000`
- Public IPs: `https://1.2.3.4:9000`

**Testing override (UNSAFE - never use in production):**

```bash
# ONLY for testing/development
LUXSWIRL_ALLOW_INSECURE_HTTP=true LUXSWIRL_SERVER_URL=http://test.example.com:9000 python -m app.main
```

**Implementation:**

- URL validation in `shared/url_security.py`
- Automatic validation during agent startup and registration
- Clear error messages with fix instructions

### Credential Protection

**Log Scrubbing:** Every log line passes through the redactor `shared/log_redaction.py:scrub`, which both services hand to the fleet logging module (`configure_logging(redact=...)`, `app/core/logging_config.py`). It scrubs the message, the traceback and every `extra=` field:

- Database connection strings (passwords masked: `mysql://user:***@host`)
- Bearer tokens (`Bearer ***`)
- API keys (`api_key=***`)
- Password fields in JSON (`"password":"***"`)

**Credential Storage (Encrypted at Rest):**

Agent credentials (`agent_id` + `api_key`) are automatically encrypted using **Fernet encryption (AES-128-CBC + HMAC)** and stored at `/app/data/agent_credentials.json`.

**How it works:**

1. **Registration:** Agent registers with server using `LUXSWIRL_AUTH_KEY` (registration key)
1. **Approval:** Server generates unique agent-specific API key; the agent fetches it with the registration key plus the per-agent recovery secret it was given at registration
1. **Storage:** Agent encrypts and saves credentials to persistent Docker volume
1. **Restart:** Agent loads and decrypts credentials automatically using container-derived key

**Encryption details:**

- **Algorithm:** Fernet (AES-128 in CBC mode + HMAC for authentication)
- **Key derivation:** PBKDF2-HMAC-SHA256 from `hostname + machine-id` (100,000 iterations)
  - Hostname: Docker container ID or system hostname
  - Machine-ID: Read from `/etc/machine-id` (persistent across container restarts)
- **File format:** Binary blob (not JSON-parseable)
- **File permissions:** Automatically set to 0600 (owner read/write only)
- **Migration:** Existing plaintext files automatically migrated on first load

**Docker usage:**

```yaml
# docker-compose.yml
services:
  agent:
    image: luxswirl-agent:latest
    volumes:
      - agent_data:/app/data  # Credentials persist here (encrypted)
    environment:
      LUXSWIRL_SERVER_URL: https://server.example.com:9000

volumes:
  agent_data:  # Persistent volume - survives container restarts
```

**Important notes:**

- Credentials **automatically encrypted** on first save (v1.0+)
- Encryption key derived from container-specific data (deterministic across restarts)
- **Container rebuild:** If hostname/machine-id changes, old credentials cannot be decrypted
  - Solution: Delete `/app/data/agent_credentials.json` and agent will re-register
- **Plaintext migration:** Legacy JSON files automatically converted to encrypted format
- **Disable encryption:** Set `LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION=true` (testing only - logs warning)

**Security properties:**

- ✅ Protected against casual file inspection
- ✅ Protected in backups and log dumps
- ✅ Automatic migration from plaintext
- ⚠️ Requires hostname + machine-id to decrypt (not secret)
- ⚠️ Does not protect against attacker with container shell access

**Troubleshooting:**

*Cannot decrypt credentials after container rebuild:*

```bash
# Error: "Failed to decrypt credentials - encryption key may have changed"
# Solution: Delete credentials and re-register
docker exec luxswirl_agent rm /app/data/agent_credentials.json
docker-compose restart agent
# Agent will detect missing credentials and re-register automatically
```

*Inspect encrypted credentials (debugging):*

```bash
# View encrypted file (binary)
docker exec luxswirl_agent cat /app/data/agent_credentials.json
# Output: gAAAAABpEjRErHp5IaxG... (Fernet-encrypted binary)

# Temporarily disable encryption for debugging (NOT for production)
docker-compose up agent -e LUXSWIRL_DISABLE_CREDENTIAL_ENCRYPTION=true
```

**Future enhancements:**

- v1.1: OS keyring support (Linux: libsecret, macOS: Keychain, Windows: DPAPI)
- v1.1: Credential vault integration (HashiCorp Vault, AWS Secrets Manager)
- v2.0: KMS integration for cloud deployments

### Check Configuration Security

**Database credentials:** Database check types (MySQL, PostgreSQL) require connection strings with passwords. Best practices:

- Use read-only database users for monitoring queries
- Restrict query permissions (SELECT only)
- Consider credential vault integration for production (planned v1.1)
- Connection strings are scrubbed from logs automatically

**Synthetic checks:** Execute arbitrary Python code via `exec()`. Security notes:

- AST validation in `core/synthetic_security.py` blocks dangerous operations
- **Use only in trusted environments** (self-hosted, controlled scripts)
- Timeout enforcement (60 seconds default)
- **Do not allow untrusted users to create synthetic checks**
- Consider isolated container execution for production (planned v2.0)

### Security Audit

A v1.0 security audit assessed risk as MODERATE → LOW after fixes, with positive findings on secure subprocess execution, SQL injection prevention, and input validation.

## Database Architecture

### Schema

See `docs/architecture/database.md` and `docs/architecture/overview.md` for detailed documentation.

**Models** (`apps/backend/app/models/`):

- `agent.py` - Agent model with relationships and computed properties
- `check.py` - Check model with agent relationship
- `check_result.py` - CheckResult model (TimescaleDB hypertable)
- `base.py` - Base classes and mixins (TimestampMixin, SoftDeleteMixin, etc.)

**Key features:**

- SQLAlchemy 2.0 with `Mapped` and `mapped_column`
- Async throughout with AsyncSession
- TimescaleDB hypertable partitioned by timestamp (1-day chunks)
- Continuous aggregate view `check_results_5min` for dashboard queries
- Automatic 90-day retention policy (configurable)
- Optimized indexes for common query patterns
- Relationships with lazy loading strategies

### Services Layer

Located in `apps/backend/app/services/core/` (business logic) and `apps/backend/app/services/views/` (template context assembly):

- **AgentService** (`agent_service.py`) - Agent CRUD, stats, online status
- **CheckService** (`check_service.py`) - Check CRUD, upsert patterns
- **CheckResultService** (`check_result_service.py`) - Results processing, history, analytics, summaries
- **MetricsService** (`metrics_service.py`) - Prometheus metrics generation

All services use SQLAlchemy 2.0 async API with asyncpg driver.

### Configuration

Set database connection via environment variables (Pydantic Settings):

```bash
export DATABASE__HOST=localhost
export DATABASE__PORT=5432
export DATABASE__NAME=luxswirl
export DATABASE__USER=luxswirl
export DATABASE__PASSWORD=luxswirl
export DATABASE__ECHO=false  # SQL query logging
```

Or use nested delimiter syntax:

```bash
export DATABASE__URL=postgresql+asyncpg://luxswirl:luxswirl@localhost:5432/luxswirl
```

## Code Style Notes

- **Async/await**: All I/O operations are async throughout the codebase
- **Type hints**: Used extensively with `typing` module and modern Python 3.10+ syntax (`str | None`)
- **Naming conventions** (enforced by `make arch`):
  - Models: `app/models/{name}_model.py`
  - Schemas: `app/schemas/{name}_schema.py`
  - CRUD: `app/crud/{name}_crud.py`
  - Core service: `app/services/core/{name}_core_service.py`
  - View service: `app/services/views/{name}_view_service.py`
  - JSON router: `app/api/v1/routers/{name}_router.py`
  - Web router: `app/web/routers/{name}_router.py`
  - Files prefixed with `_` are shared helpers (exempt from naming rule, use only when genuinely multi-consumer)
- **Imports**: `app.`-prefixed within each component (`from app.models.foo import ...`) — `apps/backend/app/` and `apps/agent/app/` are both the `app` package. Shared code is `app.shared.*` (the `apps/shared` SSOT overlaid on each leg's placeholder package). Imports go at the top of the file, enforced by `make arch` (`fw.no_inline_imports`, `shared.no_inline_imports`) — use `TYPE_CHECKING` for circular deps.
- **Docstrings**: Google style format with Args, Returns, Raises sections
- **Logging**: Structured logging via `app.shared.logger.get_logger()`
- **Error handling**:
  - Custom exceptions in `core/exceptions.py`
  - Exception handlers in `apps/backend/app/main.py`
  - Proper HTTP status codes (200, 201, 204, 404, 409, 422, 500)
- **SQLAlchemy 2.0**:
  - Use `Mapped` and `mapped_column` types
  - Async sessions with `AsyncSession`
  - `select()` for queries, not legacy Query API
- **Pydantic**:
  - BaseModel for schemas with `model_config = ConfigDict(...)`
  - Settings for configuration with `SettingsConfigDict`
  - Field validators with `@field_validator`
- **FastAPI**:
  - Dependency injection with `Depends()`
  - Router registration with `APIRouter`
  - Response models with `response_model`
  - Proper status codes with `status_code` parameter
- **Architecture** (see `docs/architecture/backend.md` for the full chain):
  - Web router → view service → core service → crud → models
  - JSON router → core service → crud → models
  - Routers do HTTP plumbing only — zero business logic, zero raw SQL, zero view-context assembly
  - View services build template context (pagination math, multi-service aggregation)
  - Core services own business logic and transactions; never touch raw SQL
  - CRUD modules are the only place that runs `db.execute(...)`, `select()`, `update()`, `delete()`, `text()`
  - View services do not import each other (helpers prefixed with `_` are exempt)
  - Enforced by `make arch` (luxarch guard) + `pyproject.toml [tool.importlinter]` (import-graph contracts)

## Recent Improvements (2024-10-22)

### Web UI - Check Detail Panel

The check detail panel has been enhanced with Uptime Kuma-style visual design:

**30-Minute Status Bar:**

- Visual timeline showing the last 30 minutes of check health
- Each bar represents one minute of aggregated check data
- Gradient styling:
  - Success: `bg-gradient-to-t from-green-600 to-emerald-400` with glow
  - Failure: `bg-gradient-to-t from-red-600 to-rose-400` with glow
  - No data: `bg-gradient-to-t from-dark-bg-tertiary to-slate-700`
- Hover effects with `scale-105` and enhanced shadows
- Tooltips showing per-minute details (count, avg latency, status)
- Implementation: `CheckDetailService` groups results by minute using `defaultdict`

**Time Range Selector:**

- Default: 4 hours of history
- Options: 4h, 8h, 24h, 3d, 7d
- HTMX-driven updates on selection change
- Dynamic labels throughout UI reflecting selected range

**Layout Structure:** Following Uptime Kuma's proven design pattern:

1. Header with status badge and time selector
1. Check metadata (name, target, type, interval)
1. Current status with pulsing indicator dot
1. 30-minute visual status bar
1. 2×2 statistics grid (current response, avg response, uptime %, total checks)
1. Performance chart (Chart.js with latency over time)
1. Recent events accordion (collapsed by default)

### Web UI - Status Dashboard Stability

**Fixed: Check Ordering Inconsistency**

- **Problem**: Checks appeared in different order on each HTMX refresh
- **Root cause**: Query sorted by `check_name` only, causing unstable sort for duplicate names across agents
- **Solution**: Changed to `ORDER BY agent_id, check_name` for deterministic ordering
- **File**: `apps/backend/app/services/views/status_view_service.py:153`

**Fixed: Checks Randomly Disappearing**

- **Problem**: Checks like "check_web" would appear/disappear on refresh
- **Root cause**: Query only looked at results from last 5 minutes (`cutoff_recent`)
  - Checks with intervals > 5 minutes would have no recent results
  - Status filter would then exclude them as "unknown"
- **Solution**: Removed time window on latest results query
  - Now gets most recent result regardless of age
  - Checks always appear with their last known status
- **Files**:
  - `apps/backend/app/services/views/status_view_service.py:117` (removed `cutoff_recent`)
  - `apps/backend/app/services/views/status_view_service.py:177` (removed timestamp filter)

**Technical Details:**

- Uses window functions (`row_number() OVER (PARTITION BY check_id ORDER BY timestamp DESC)`)
- Batch queries minimize database round-trips
- Stable sort ensures consistent UI presentation
- Status shown even if check hasn't run recently

### Web UI - Visual Enhancements

- Pulsing status indicator dots (`animate-pulse`)
- Gradient backgrounds with inner shadows
- Smooth transitions on hover (`duration-200`)
- Professional dark mode optimized for monitoring
- Responsive design with Tailwind utility classes

See `CHANGELOG.md` for complete change history.

## Recent Improvements (2025-11-07/08)

### Check Type Metrics Enhancement

All database and DNS check types now use the standardized `metrics` parameter for storing check-specific data:

**Pattern:**

```python
return self.create_result(
    success=success,
    latency_ms=total_latency_ms,
    error=error,
    metrics={
        "check_type": {
            # Check-specific metrics here
        }
    }
)
```

**DNS Checks** (`checks/dns.py`):

- Stores: TTL, authoritative flag, recursion flags, record count, records, canonical name
- UI displays DNS Query Details section with blue gradient theme
- Shows nameserver, record type, flags, and all returned records

**MySQL Checks** (`checks/mysql.py`):

- Stores: connection_latency_ms, query_latency_ms, row_count, columns, error_type
- UI displays MySQL Query Details section with orange/yellow gradient
- Shows performance breakdown and query metadata

**PostgreSQL Checks** (`checks/postgres.py`):

- Stores: connection_latency_ms, query_latency_ms, row_count, columns, error_type
- UI displays PostgreSQL Query Details section with indigo/blue gradient
- Shows performance breakdown and query metadata

**UI Template** (`templates/web/partials/panels/checks/check_detail_panel.html`):

- Lines 202-289: DNS section
- Lines 291-357: MySQL section
- Lines 359-425: PostgreSQL section
- All sections follow consistent design pattern with gradient themes and performance grids

### Example Check: Self-Monitoring

Created PostgreSQL check for monitoring LuxSwirl's own database:

```sql
-- Check: luxswirl_database_health
-- Target: postgresql://luxswirl:luxswirl@timescaledb:5432/luxswirl
-- Query: SELECT COUNT(*) as total_results FROM check_results
-- Interval: 60 seconds
-- Purpose: Dogfooding - monitor LuxSwirl with LuxSwirl
```

### Architecture Notes

**Metrics Storage:**

- Check results store type-specific data in `metrics` JSONB column
- Template accesses via `history[0].additional_data.{check_type}`
- Services map database JSONB to Pydantic schemas for validation
- UI renders metrics in themed sections matching check type

**Registry Pattern:** Both checks and notification providers use registry pattern:

- `agent.register_check_type()` for check types
- `NotificationRegistry.register()` for notification providers
- Enables easy extensibility without modifying core code
