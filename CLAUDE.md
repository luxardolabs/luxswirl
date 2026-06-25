# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

<!-- Maintainer-only working notes (roadmap, internal tooling, operational
     snapshots) live outside the repo and are imported below. The import is a
     no-op for anyone who doesn't have the file (e.g. public clones). -->
@~/.claude/projects/luxswirl.md

## Project Overview

**LuxSwirl** is a production-quality SaaS observability monitoring platform for all types of metrics developed by Luxardo Labs. It consists of three main components:
- **Agent**: Executes health checks (ping, HTTP, TCP, JSON) and reports results to the server
- **Server**: FastAPI REST API server with production-grade architecture (models, schemas, services, routers)
- **TimescaleDB**: PostgreSQL extension providing time-series storage with automatic partitioning and retention

The system is designed as a professional SaaS platform with:
- Complete separation of concerns (models, schemas, services, routers)
- Async/await patterns throughout for high performance
- Pydantic Settings for type-safe configuration
- SQLAlchemy 2.0 with async support
- TimescaleDB hypertables for efficient time-series storage
- API versioning (/api/v1) for future compatibility
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
│   │   └── agent_main.py            # Agent entrypoint (python -m app.agent_main)
│   ├── tests/
│   ├── pyproject.toml + poetry.lock # Agent deps only (playwright, db drivers, …)
│   └── Dockerfile + Dockerfile.lint
└── shared/                          # config, logger, url_security, subprocess_safe, jobs/
                                     # — vendored into BOTH images at build (COPY apps/shared)

VERSION                              # version source of truth (NOT derived from path)
Makefile                             # build/dev/prod/lint/test — everything runs in docker
compose*.yaml                        # base + dev/prod/agent/test overlays
```

Each component is its own deployable: `apps/backend/app/` and `apps/agent/app/` are both the **`app` package**, so imports are **`app.`-prefixed** (`from app.models.foo import ...`). `shared.*` stays a flat top-level import (vendored into each image). Because both components use the name `app`, they have separate pyprojects/pytest configs and **cannot** share one test run. One Dockerfile per component; `pyproject.toml` `pythonpath` wires pytest (`["." , ".." ]` → resolves `app.*` and `shared.*`).

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

**Enforcement:** `tests/test_architecture.py` (grep-based checks for raw SQL, naming, LOC limits, business logic in routers) + `pyproject.toml [tool.importlinter]` (6 import-graph contracts). Both run in the same pytest suite.

### Three-component runtime

1. **luxswirl_agent** (`apps/agent/`) — Runs health checks. Entry: `apps/agent/app/agent_main.py` (`python -m app.agent_main`). Pushes results to server via HTTP. Built as its own image.
2. **luxswirl_server** (`apps/backend/`) — FastAPI app: REST API at `/api/v1`, web dashboard at `/dashboard`, Prometheus at `/metrics`. Entry: `apps/backend/app/main.py` (`uvicorn app.main:app`). Built as its own image.
3. **TimescaleDB** — PostgreSQL extension. Automatic partitioning, configurable retention, continuous aggregates. Schema lives in `apps/backend/app/models/*_model.py`; DDL helpers live in `apps/backend/app/crud/timescale_crud.py`.

### Web UI

**Stack:** HTMX (10-second polling) + Tailwind CSS 3.4 (dark-mode theme) + Chart.js + Jinja2.

**Layout:**
```
apps/backend/app/
├── web/routers/                     # HTMX/Jinja route handlers (HTTP plumbing only)
├── web/templates/
│   ├── pages/                       # Full pages
│   ├── partials/                    # HTMX swap targets
│   └── macros/                      # Reusable Jinja components
├── web/static/                      # Tailwind output, JS, images
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
- **Admin Role Required**: Only admins create/modify synthetic checks — enforced in the core service (`CheckCoreService.create_check/update_check/clone_check`), not just the web layer, so the JSON API can't bypass it. Web users are gated by RBAC role; the JSON API has no role model, so any API Bearer token is admin-equivalent.
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
2. Implement the `run()` method returning a result dictionary
3. Register it in `apps/agent/app/agent_main.py` using `agent.register_check_type()`

### Configuration System

Configuration is handled by `shared/config.py` (top-level shared module on `sys.path`):
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
- Background task processes stored reports when server becomes available
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

Two **separate** steps — `build-dev` builds, `dev-up` brings up. They are decoupled on purpose: `dev-up` does NOT build.

```bash
make build-dev     # BUILD ONLY: (re)build both :dev images from current source (baked, no source mounts)
make dev-up        # START ONLY: `compose up -d` — does NOT build; recreates onto whatever :dev image is on disk
make dev-restart   # restart the running containers ONLY — no rebuild, does NOT pick up code changes
make dev-rebuild   # `compose build --no-cache` then up — full from-scratch rebuild
make dev-logs      # follow logs
make dev-shell     # bash into the server container
make dev-down      # stop the stack (volume preserved)
```

Dev runs **baked images** (code COPYd in — no source bind-mounts). **To pick up a code change you MUST `make build-dev` FIRST, then `make dev-up`** (chain them: `make build-dev && make dev-up`). `make dev-up` alone only recreates the container onto the existing `:dev` image — it will run STALE code if you didn't build first. `make dev-restart` just bounces the containers (also stale). The `luxswirl_db_data` volume persists across `down`/`up`; only `make clean-all` (`down -v`) drops it.

### Build & release

```bash
make version                # show VERSION + image tags
make build                  # build both images as :$(VERSION) (no push)
make build-dev              # build both as :dev (baked version) for the dev stack
make push                   # build + push both :$(VERSION) AND :latest
make release                # css + push (promote a release)
```

Version is **coded in the `VERSION` file** (not derived from the directory path) → baked via `--build-arg BUILD_VERSION` → `ENV APP_VERSION`/`LABEL version` → `settings.app_version` → `/health`. Registry: `ghcr.io/luxardolabs/luxswirl-{backend,agent}` (override `REGISTRY` to push elsewhere).

### Quality & tests (all dockerized)

```bash
make lint          # ruff check + format-check + mypy for BOTH components (fresh Dockerfile.lint images)
make test          # backend suite against an isolated test DB (compose.test.yaml)
make arch          # architecture guards (grep tests + import-linter contracts)
make gitleaks      # secret scan (must pass before the first git commit)
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
- `auth_tokens`: List of valid Bearer tokens for authentication

## API Endpoints

### Server REST API (v1)

**Agents**
- `GET /api/v1/agents` - List all agents with pagination and filtering
- `GET /api/v1/agents/{agent_id}` - Get specific agent details
- `POST /api/v1/agents` - Create a new agent
- `PATCH /api/v1/agents/{agent_id}` - Update agent metadata
- `DELETE /api/v1/agents/{agent_id}` - Delete agent and all data
- `GET /api/v1/agents/{agent_id}/stats` - Get agent statistics

**Checks**
- `GET /api/v1/agents/{agent_id}/checks` - List checks for an agent
- `GET /api/v1/agents/{agent_id}/checks/{check_name}` - Get specific check
- `POST /api/v1/agents/{agent_id}/checks` - Create a new check
- `POST /api/v1/agents/{agent_id}/checks/{check_id}/clone` - Clone an existing check with optional field overrides
- `PATCH /api/v1/agents/{agent_id}/checks/{check_name}` - Update check config
- `DELETE /api/v1/agents/{agent_id}/checks/{check_name}` - Delete check

**Check Results**
- `POST /api/v1/reports` - Submit agent report (bulk check results)
- `GET /api/v1/agents/{agent_id}/results` - Get latest results for agent
- `GET /api/v1/agents/{agent_id}/checks/{check_name}/history` - Get check history
- `GET /api/v1/agents/{agent_id}/checks/{check_name}/summary` - Get check statistics
- `GET /api/v1/stats` - Get global aggregated statistics

**Metrics & Health**
- `GET /metrics` - Prometheus-format metrics export
- `GET /metrics/summary` - Metrics summary (JSON)
- `GET /health` - Server health check
- `GET /` - API information
- `GET /docs` - Auto-generated OpenAPI documentation (Swagger UI)
- `GET /redoc` - Auto-generated ReDoc documentation

All endpoints require Bearer token authentication via the `Authorization` header.

### Prometheus Metrics

The `/metrics` endpoint exposes:
- `luxswirl_check_success` - Whether check succeeded (1/0)
- `luxswirl_check_up` - Whether check is still reporting (1/0)
- `luxswirl_check_latency_seconds` - Check latency in seconds
- `luxswirl_check_last_execution_time` - Timestamp of last execution
- `luxswirl_agent_up` - Whether agent is reporting (1/0)

All metrics include labels: `agent`, `check`, `type`, `target`

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
LUXSWIRL_ALLOW_INSECURE_HTTP=true LUXSWIRL_SERVER_URL=http://test.example.com:9000 python -m app.agent_main
```

**Implementation:**
- URL validation in `shared/url_security.py`
- Automatic validation during agent startup and registration
- Clear error messages with fix instructions

### Credential Protection

**Log Scrubbing:** All loggers automatically scrub sensitive credentials using `CredentialFilter` (`shared/logger.py`):
- Database connection strings (passwords masked: `mysql://user:***@host`)
- Bearer tokens (`Bearer ***`)
- API keys (`api_key=***`)
- Password fields in JSON (`"password":"***"`)

**Credential Storage (Encrypted at Rest):**

Agent credentials (`agent_id` + `api_key`) are automatically encrypted using **Fernet encryption (AES-128-CBC + HMAC)** and stored at `/app/data/agent_credentials.json`.

**How it works:**
1. **Registration:** Agent registers with server using `LUXSWIRL_AUTH_KEY` (registration key)
2. **Approval:** Server generates unique agent-specific API key
3. **Storage:** Agent encrypts and saves credentials to persistent Docker volume
4. **Restart:** Agent loads and decrypts credentials automatically using container-derived key

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

A v1.0 security audit assessed risk as MODERATE → LOW after fixes, with positive
findings on secure subprocess execution, SQL injection prevention, and input validation.

## Database Architecture

### Schema

See `docs/architecture/DATABASE.md` and `docs/architecture/overview.md` for detailed documentation.

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
- **Naming conventions** (enforced by `test_architecture.py`):
  - Models: `app/models/{name}_model.py`
  - Schemas: `app/schemas/{name}_schema.py`
  - CRUD: `app/crud/{name}_crud.py`
  - Core service: `app/services/core/{name}_core_service.py`
  - View service: `app/services/views/{name}_view_service.py`
  - JSON router: `app/api/v1/routers/{name}_router.py`
  - Web router: `app/web/routers/{name}_router.py`
  - Files prefixed with `_` are shared helpers (exempt from naming rule, use only when genuinely multi-consumer)
- **Imports**: `app.`-prefixed within each component (`from app.models.foo import ...`) — `apps/backend/app/` and `apps/agent/app/` are both the `app` package. `shared.*` is a flat top-level import (vendored into each image at build). The repo enforces top-of-file imports via a PreToolUse hook (`.claude/hooks/no-inline-imports.py`) — use `TYPE_CHECKING` for circular deps.
- **Docstrings**: Google style format with Args, Returns, Raises sections
- **Logging**: Structured logging via `shared.logger.get_logger()`
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
  - Enforced by `tests/test_architecture.py` (grep) + `pyproject.toml [tool.importlinter]` (import-graph)

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

**Layout Structure:**
Following Uptime Kuma's proven design pattern:
1. Header with status badge and time selector
2. Check metadata (name, target, type, interval)
3. Current status with pulsing indicator dot
4. 30-minute visual status bar
5. 2×2 statistics grid (current response, avg response, uptime %, total checks)
6. Performance chart (Chart.js with latency over time)
7. Recent events accordion (collapsed by default)

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

**UI Template** (`web/templates/partials/check_detail_panel.html`):
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

**Registry Pattern:**
Both checks and notification providers use registry pattern:
- `agent.register_check_type()` for check types
- `NotificationRegistry.register()` for notification providers
- Enables easy extensibility without modifying core code
