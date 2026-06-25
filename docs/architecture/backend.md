# Backend Architecture

This is the canonical reference for the LuxSwirl backend layout and layering rules. Everything is enforced by `tests/test_architecture.py` (grep-based) plus `pyproject.toml` `[tool.importlinter]` (import-graph based) — both run as part of the same pytest suite.

## Layering chain

```
JSON API:  api/v1/routers/*_router.py  →  services/core/*_core_service.py  →  crud/*_crud.py  →  models/*_model.py
Web UI:    web/routers/*_router.py     →  services/views/*_view_service.py →  services/core/*_core_service.py  →  crud/*_crud.py  →  models/*_model.py
```

**Rules:**

- **Routers** (both API and Web) only do HTTP plumbing: parse query/form params, call exactly one service method, hand the result to the response/template, translate exceptions if needed. **Zero business logic, zero view-context assembly, zero raw SQL.** Global `LuxSwirlException` handler in `main.py` converts custom exceptions to status codes — routers do not catch and re-raise as `HTTPException`.
- **Web routers** must go through a **view service**. They cannot import `services.core` or `crud` directly.
- **API routers** must go through a **core service**. They cannot import `services.views` or `crud` directly.
- **View services** assemble template context. They call core services and CRUD as needed but do not duplicate business logic. **View services do not import other view services** — except for underscore-prefixed helpers.
- **Core services** own business logic, transactions (`db.commit/flush`), and orchestration. They never construct raw SQL — all data access goes through CRUD.
- **CRUD** modules are the only place that calls `db.execute(...)`, `select()`, `update()`, `delete()`, or `text()`. They never import services or routers.
- **Models** are SQLAlchemy ORM only. They may import other models for relationships.

## File naming (enforced)

| Layer            | Path pattern                                    |
|------------------|-------------------------------------------------|
| ORM model        | `app/models/{name}_model.py`                    |
| Pydantic schema  | `app/schemas/{name}_schema.py`                  |
| CRUD             | `app/crud/{name}_crud.py`                       |
| Core service     | `app/services/core/{name}_core_service.py`     |
| View service     | `app/services/views/{name}_view_service.py`    |
| JSON API router  | `app/api/v1/routers/{name}_router.py`           |
| Web/HTMX router  | `app/web/routers/{name}_router.py`              |

Files starting with `_` (e.g. `_dashboard_render.py`) are **shared helpers** and are exempt from the naming rule. Use them only when the helper is genuinely consumed by multiple services. Single-consumer "helpers" should be inlined into their consumer.

## Imports

`apps/backend/app/` and `apps/agent/app/` are both the `app` package, so imports are **`app.`-prefixed within each component** (`from app.models.foo import ...`, `from app.crud.bar import ...`). `shared.*` stays a flat top-level import (vendored into each image at build). `pyproject.toml` sets `pythonpath = [".", "..", "tests"]` so pytest resolves `app.*` and `shared.*`; the Dockerfile sets the same at runtime. Top-of-file imports are enforced by a PreToolUse hook — use `TYPE_CHECKING` for circular deps.

## Where business logic vs. HTTP plumbing lives

| Concern                                              | Layer            |
|------------------------------------------------------|------------------|
| Token verification, dependency injection             | Router (HTTP)    |
| Form/query parameter unpacking                       | Router (HTTP)    |
| Pagination math (`total_pages`, `has_prev`)          | View service     |
| Default page-size fallback from settings             | View service     |
| Template context dict assembly                       | View service     |
| Aggregating multiple core service calls for a page   | View service     |
| Permission/state-machine business rules              | Core service     |
| Multi-table writes / transactions / commits          | Core service     |
| Raw SQL, `select()`, `update()`, `delete()`, `text()` | CRUD             |
| Hypertable DDL (`add_retention_policy`, etc.)        | CRUD             |

## Running the architecture checks

Everything runs in Docker via the Makefile (the only host requirements are `make` + `docker`):

```bash
make arch       # architecture guards: grep tests + import-linter contracts
make lint       # ruff check + format-check + mypy for both components
make test       # backend test suite against an isolated test DB
make compileall # fast byte-compile syntax check
```

Run `make arch` (or `make check`, which is lint + test) after every multi-file refactor before pushing.

## Adding a new feature: where does it go?

1. New table → `models/{X}_model.py` (single class, ORM only)
2. Wire data access → `crud/{X}_crud.py` (static methods, all `db.execute` lives here)
3. Business logic / transactions → `services/core/{X}_core_service.py`
4. JSON endpoint → `api/v1/routers/{X}_router.py` calling the core service
5. UI page → view service `services/views/{X}_view_service.py` building a context dict, plus `web/routers/{X}_router.py` rendering a Jinja template with that context
6. Pydantic schemas for request/response → `schemas/{X}_schema.py`

If steps 4 and 5 both exist, the **router can never reach below the immediately-next layer**. Web router calls view service; view service calls core service; core service calls CRUD. Skipping a layer is a contract violation and import-linter will fail the build.

## Why both grep + import-linter?

- **Grep** (`test_architecture.py`) catches concrete code smells: raw `select()` in a service, `BaseModel` defined in a router, files exceeding the LOC budget, files violating the naming convention. It enforces the *what*.
- **Import-linter** catches dependency-direction violations: web router accidentally importing crud, view service composing another view service, core service reaching into web. It enforces the *how things connect*.

Both pass in CI. Both should pass locally before pushing.
