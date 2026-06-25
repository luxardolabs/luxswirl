# Contributing to LuxSwirl

LuxSwirl is released as an open-source give-back. Contributions are welcome but not expected, and they aren't actively solicited. This file exists mainly so anyone who finds the tool useful can run it locally and understand how it's put together before changing it.

If you do send a patch: fork, branch, open a PR. A good PR is small, focused, has a clear description of what changed and why, keeps the existing layering (below), and passes `make check`. That's it — no templates to fill out.

---

## Development Setup

### Prerequisites

- **Docker** and **Docker Compose** — everything (build, lint, tests, dev stack) runs in containers, so you don't need host Python or Poetry for the normal workflow.
- **Git**
- The backend/agent target **Python 3.14** (`^3.14` in `pyproject.toml`); only relevant if you run a component directly on the host (Option 2 below).

### Clone

```bash
git clone https://github.com/luxardolabs/luxswirl.git
cd luxswirl
```

Dependencies are managed per component with Poetry, resolved inside docker. To verify both components resolve and install cleanly from their lockfiles:

```bash
make poetry-install
```

### Start the dev stack

**Option 1: Full dev stack (recommended)** — everything in docker, baked images:

```bash
make dev-up      # build :dev images (version baked) + start the full stack
make dev-logs    # follow logs
make dev-shell   # bash into the server container
make dev-down    # stop (DB volume preserved)
```

Dev runs **baked images** (no source mounts), so re-run `make dev-up` after code changes to rebuild and recreate. Other handy targets: `make dev-restart`, `make dev-rebuild`, `make dev-agent-shell`, `make status`, `make logs`.

**Option 2: Run a component directly** (without the full stack) — from its dir, with Poetry on the host:

```bash
# Start the database only
docker compose -f compose.yaml -f compose.dev.yaml up -d timescaledb

# Server (from apps/backend)
cd apps/backend && poetry install
DATABASE__URL=postgresql+asyncpg://luxswirl:luxswirl@localhost:5432/luxswirl \
  PYTHONPATH=.:.. poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 9000

# Agent (separate terminal, from apps/agent)
cd apps/agent && poetry install
LUXSWIRL_SERVER_URL=http://localhost:9000/api/v1/reports \
  PYTHONPATH=.:.. poetry run python -m app.agent_main
```

**Access:**

- Web UI: http://localhost:9000
- API docs: http://localhost:9000/docs (Swagger UI)
- Database: `psql -h localhost -U luxswirl -d luxswirl` (password: `luxswirl`), or `make db-shell`

### Database migrations

Schema is managed by Alembic. Migrations live in `apps/backend/alembic/versions/`.

**Adding a schema change:**

1. Edit the relevant model in `apps/backend/app/models/*_model.py`.
2. Generate a migration:
   ```bash
   docker exec -w /app luxswirl_server alembic revision --autogenerate -m "describe_change"
   ```
3. Open the generated file under `apps/backend/alembic/versions/` and review it. Autogenerate handles vanilla DDL (CREATE/ALTER/DROP table, columns, indexes, FKs) but does NOT detect:
   - CHECK constraints
   - Comment changes
   - Server-default expression changes
   - ENUM modifications
   - TimescaleDB-specific operations (hypertables, compression, retention, continuous aggregates — those live in `db/database.py`)
   - Custom SQLAlchemy `TypeDecorator` types (e.g. `EncryptedString`) — autogen references them by name and forgets to import. Replace with the underlying DB-level type (`sa.String`, `sa.Text`, etc.) since the DB doesn't see the decorator.
4. Rename the file with a numeric prefix matching the next sequence: `001_<slug>.py`, `002_<slug>.py`. The internal `revision = '<hash>'` stays as autogen produced it.
5. Restart the server. The container CMD chain runs `alembic upgrade head` before uvicorn, so the migration applies automatically. Failure exits the container loudly.
6. Test downgrade: `alembic downgrade -1` then `alembic upgrade head` should round-trip cleanly.

**Local CLI access (without docker exec):** runs from `apps/backend/` (where `alembic.ini` lives; `PYTHONPATH=.:..` so alembic's `env.py` can import `app.models` + `shared`):

```bash
cd apps/backend
PYTHONPATH=.:.. DATABASE__URL=postgresql+asyncpg://luxswirl:luxswirl@localhost:5432/luxswirl poetry run alembic upgrade head
```

### Tests, lint, and the full check

Everything runs in docker via the Makefile — no host tools needed.

```bash
make test     # backend test suite against an isolated test DB (spun up + torn down)
make lint     # ruff check + format-check + mypy, for BOTH components
make format   # ruff auto-fix + format (writes changes back to the tree)
make arch     # architecture guards (grep tests + import-linter contracts)
make check    # the full pre-commit suite: lint + types + tests
```

Run one test file with `make test TEST=tests/test_checks.py`.

---

## Coding Standards

Linting and formatting are enforced by `make lint` / `make format` (ruff + mypy). Type hints are required; line length and quote style are whatever ruff is configured for — run `make format` and don't fight it.

### Layered architecture

The backend is split into layers, and the boundaries are **enforced in CI** by import-linter contracts (`[tool.importlinter]` in `apps/backend/pyproject.toml`) and by the grep-based guards in `apps/backend/tests/test_architecture.py`. Both run under `make arch` / `make check`. There are zero exemptions — a violation fails the build.

The layering, from outermost to innermost:

```
router  →  view service  →  core service  →  crud  →  models
```

Each layer may only import from layers below it, with these added rules:

- **Web routers** (`app/web/routers/`, HTMX) go through **view services** — they must not import core services or crud directly.
- **JSON API routers** (`app/api/v1/routers/`) go through **core services** — they must not import view services or crud directly.
- **View services** (`app/services/views/`) assemble HTTP/template responses. They do not compose other view services.
- **Core services** (`app/services/core/`) hold HTTP-agnostic business logic. They must not import view services, routers, or anything web-facing.
- **CRUD** (`app/crud/`) is the only layer that talks to the database via SQLAlchemy. It must not import services or routers.
- **Models** (`app/models/`) are SQLAlchemy ORM definitions only — schema, relationships, constraints. No business logic.

Pydantic **schemas** (`app/schemas/`) handle API request/response validation and serialization; they hold no database access.

### File naming

The architecture guards also enforce one-suffix-per-layer naming (again, zero exemptions):

| Layer | Location | File suffix |
| --- | --- | --- |
| Models | `app/models/` | `{x}_model.py` (except `base.py`, `enums.py`) |
| Schemas | `app/schemas/` | `{x}_schema.py` |
| CRUD | `app/crud/` | `{x}_crud.py` |
| Core services | `app/services/core/` | `{x}_core_service.py` |
| View services | `app/services/views/` | `{x}_view_service.py` |
| JSON API routers | `app/api/v1/routers/` | `{x}_router.py` |
| Web routers | `app/web/routers/` | `{x}_router.py` |

A bare `{x}_service.py` (no `core`/`view` qualifier) is a naming violation and will fail `make arch`.

### Other conventions

- **Async throughout** — database queries, HTTP calls, and service/crud methods are `async`. Don't introduce blocking I/O (e.g. `requests`) into the event loop.
- **Structured logging** via `shared.logger.get_logger(__name__)`; pass fields through `extra=` rather than interpolating them into the message string.
- **Security**: validate API inputs with Pydantic schemas; rely on the ORM's parameterized queries (never string-concatenate SQL); never log credentials (the `CredentialFilter` masks them); use list-form `subprocess.run([...])`, never `shell=True` with user input.

---

## Contributing and Licensing

LuxSwirl is licensed under AGPLv3, and contributions follow the standard inbound = outbound model: **by submitting a pull request, you license your contribution under AGPLv3** — the same license as the rest of the project. You keep the copyright to your own work and you're credited in the git history.

There's **no CLA** to sign, and no dual-licensing or commercial-licensing program — LuxSwirl is a give-back to the open-source community.
