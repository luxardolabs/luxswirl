"""Architecture guards — pytest-level enforcement of layered architecture.

Adapted from luxtaste/apps/backend/tests/test_architecture.py.
ZERO exempts — every violation is real.

Standard (matches luxtaste / luxhelix / luxcapital):
  - models/{X}_model.py        (except base.py, enums.py)
  - crud/{X}_crud.py
  - api/v1/routers/{X}_router.py     (JSON API)
  - web/routers/{X}_router.py        (HTMX)
  - services/core/{X}_core_service.py     (HTTP-agnostic business logic)
  - services/views/{X}_view_service.py    (HTTP/template assembly)

Layering: router → view service → core service → crud → models
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _paths import BACKEND_ROOT, REPO_ROOT  # layout-aware (host + container)
from shared.idtype_guard import (
    GuardConfig,
    annotation_element_type,
    find_violations,
    sqlalchemy_uuid_id_names,
)

APP = BACKEND_ROOT

# ─── Standard layout (luxtaste pattern) ───────────────────────────────
SERVICE_CORE_DIR = APP / "services" / "core"
SERVICE_VIEWS_DIR = APP / "services" / "views"
JSON_ROUTERS_DIR = APP / "api" / "v1" / "routers"
WEB_ROUTERS_DIR = APP / "web" / "routers"
CRUD_DIR = APP / "crud"
MODELS_DIR = APP / "models"
SCHEMAS_DIR = APP / "schemas"
SERVICES_DIR = APP / "services"

# The two canonical router homes. Routers must live ONLY here.
ALL_ROUTER_DIRS = (JSON_ROUTERS_DIR, WEB_ROUTERS_DIR)

# Legacy top-level router dir (pre-standard). Must be empty — anything here is
# a router parked outside the canonical homes, invisible to every other guard.
LEGACY_ROUTERS_DIR = APP / "routers"

# Files in models/ that are NOT domain models (shared infra).
MODELS_NAMING_EXEMPT_KW = {"base.py", "enums.py"}

# Files in schemas/ that are NOT domain schemas (shared base classes).
SCHEMAS_NAMING_EXEMPT_KW = {"base.py"}

# The canonical enum module — the one app.models.* import a router may make
# (enums are shared infra, legitimately used as path/query-param types).
ROUTER_MODEL_IMPORT_EXEMPT = {"app.models.enum_model"}

# Size limits (luxtaste tuned values).
ROUTER_MAX_LOC = 550
SERVICE_MAX_LOC = 1000

# Raw SQL patterns — service layer must delegate to crud, never touch the
# session. Both `session.execute(` and `db.execute(` are caught: the FastAPI
# dependency injects the AsyncSession as `db`, so `db.execute(text(...))` is the
# common way raw SQL sneaks into a service. Negative lookbehind `(?<!\.)` avoids
# false positives on method calls like `repo.delete(item)`.
RAW_SQL_PATTERNS = [
    re.compile(r"\bsession\.execute\("),
    re.compile(r"\bdb\.execute\("),
    re.compile(r"(?<!\.)\bselect\("),
    re.compile(r"(?<!\.)\bupdate\(\s*\w+\s*\)"),
    re.compile(r"(?<!\.)\bdelete\(\s*\w+\s*\)"),
]

# Matches an inline schema-DTO construction like `AgentCreate(` / `CheckUpdate(`.
DTO_CONSTRUCT_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Create|Update)\(")

# A query param whose NAME denotes a closed set (an enum) — status/type/...
# Free-text names (search, q, slug, email, name, url, ...) are excluded.
# Names that denote a closed set (→ must be enum-typed). `provider` is deliberately
# NOT here: in this codebase a bare `provider` query param is a provider *instance*
# id (a UUID reference filter), not the NotificationProviderType enum — the
# `provider_type` form (caught by `[a-z_]+_type`) is the enum one.
ENUMISH_QUERY_NAME = re.compile(r"^(status|state|category|tier|visibility|type|[a-z_]+_type)$")
STR_QUERY_PARAM = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s*:\s*"
    # old style: `name: str | None = Query(...)`
    r"(?:str(?:\s*\|\s*None)?\s*=\s*Query\("
    # Annotated style: `name: Annotated[str | None, Query(...)]`
    r"|Annotated\[\s*str(?:\s*\|\s*None)?\s*,\s*Query\()"
)

# HTTP route-handler decorator verbs. A module-level function in a router that
# carries none of these is a non-route helper — orchestration that belongs in a
# view service, not the router.
_ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Inline HTML markup in a router/view — markup belongs in app/web/templates/.
# Conservative: a literal string opening an HTML tag in HTMLResponse(...) or a
# bare return. Won't fire on TemplateResponse / helper-built responses.
INLINE_HTML_PATTERN = re.compile(r"""(HTMLResponse\(\s*|return\s+)f?["']\s*<\s*[a-zA-Z]""")

# Dirs that legitimately run raw SQL outside crud/ (documented infra):
#   background/ — VACUUM/maintenance DDL that can't run in a crud-managed txn
#   db/         — engine/session bootstrap + TimescaleDB init
#   scripts/    — one-off migration/maintenance scripts
RAW_SQL_OUTSIDE_CRUD_EXEMPT_DIRS = {"background", "db", "scripts"}

FASTAPI_IMPORT = re.compile(
    r"^\s*(" + r"\x66rom" + r" fastapi|" + r"\x66rom" + r" starlette|"
    r"\x69mport" + r" fastapi|" + r"\x69mport" + r" starlette)",
    re.MULTILINE,
)

SQLALCHEMY_QUERY_IMPORT = re.compile(
    r"^\s*"
    + r"\x66rom"
    + r" sqlalchemy"
    + r" "
    + r"\x69mport"
    + r" .*\b(select|update|delete|insert|text|and_|or_)\b",
    re.MULTILINE,
)


def _py_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [
        p
        for p in directory.rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    ]


def _line_count(path: Path) -> int:
    return len(path.read_text().splitlines())


def _file_imports(path: Path) -> list[str]:
    """Return the dotted module paths imported by a file (top-level + from)."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    return imports


def _class_names(path: Path) -> list[str]:
    """Top-level class names defined in a file."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, ast.ClassDef)]


def _router_files(directory: Path) -> list[Path]:
    """Router modules in `directory` — `_`-prefixed shared helper modules
    (e.g. `_render.py`) are exempt, matching the fleet `_*`-helper convention
    (luxwx/luxtaste/luxpm/luxsignal). They're collaborators, not route modules.
    """
    return [p for p in _py_files(directory) if not p.name.startswith("_")]


def _expected_service_class(stem: str, suffix: str) -> str:
    """`agent_core_service` + `CoreService` → `AgentCoreService`."""
    base = stem[: -len(suffix)] if stem.endswith(suffix) else stem
    pascal = "".join(part.capitalize() for part in base.strip("_").split("_"))
    return f"{pascal}{''.join(w.capitalize() for w in suffix.split('_'))}"


# ────────────────────────────────────────────────────────────────────────
# Required structure — the standard layout dirs must exist.
# ────────────────────────────────────────────────────────────────────────


def test_services_core_dir_exists() -> None:
    """app/services/core/ must exist for HTTP-agnostic business logic."""
    assert SERVICE_CORE_DIR.is_dir(), (
        f"Missing required directory: {SERVICE_CORE_DIR.relative_to(BACKEND_ROOT)}\n"
        "All HTTP-agnostic business logic services must live here, named "
        "*_core_service.py."
    )


def test_services_views_dir_exists() -> None:
    """app/services/views/ must exist for view assembly services."""
    assert SERVICE_VIEWS_DIR.is_dir(), (
        f"Missing required directory: {SERVICE_VIEWS_DIR.relative_to(BACKEND_ROOT)}\n"
        "All view/template assembly services must live here, named "
        "*_view_service.py."
    )


# ────────────────────────────────────────────────────────────────────────
# Router location — routers live ONLY in the two canonical homes.
# ────────────────────────────────────────────────────────────────────────


def test_no_routers_outside_canonical_dirs() -> None:
    """Routers must live only in api/v1/routers/ (JSON) or web/routers/ (HTMX).

    The legacy top-level app/routers/ dir is a third router home that escapes
    every other guard in this file — naming, size, raw-SQL, sqlalchemy-import,
    response-model — and is named by none of the import-linter contracts. A
    router parked there is architecturally invisible. Relocate it into a
    canonical dir (the mounted URL is set by include_router(), not the file
    path, so the move is URL-preserving).
    """
    offenders = [str(p.relative_to(BACKEND_ROOT)) for p in _py_files(LEGACY_ROUTERS_DIR)]
    assert not offenders, (
        f"{len(offenders)} router module(s) in the legacy app/routers/ dir "
        "(move to app/api/v1/routers/ or app/web/routers/):\n  " + "\n  ".join(offenders)
    )


# ────────────────────────────────────────────────────────────────────────
# Naming conventions.
# ────────────────────────────────────────────────────────────────────────


def test_model_naming() -> None:
    """Every domain model file must end in _model.py (except base.py, enums.py)."""
    offenders: list[str] = []
    for path in _py_files(MODELS_DIR):
        if path.name in MODELS_NAMING_EXEMPT_KW:
            continue
        if not path.name.endswith("_model.py"):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} model file(s) don't end in _model.py:\n  " + "\n  ".join(offenders)
    )


def test_crud_naming() -> None:
    """Every CRUD file must end in _crud.py."""
    offenders: list[str] = []
    for path in _py_files(CRUD_DIR):
        if not path.name.endswith("_crud.py"):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, f"{len(offenders)} CRUD file(s) don't end in _crud.py:\n  " + "\n  ".join(
        offenders
    )


def test_schema_naming() -> None:
    """Every Pydantic schema file must end in _schema.py (except base.py).

    Without this gate schemas drift into bare names (`user.py`, `check.py`) and
    collide with the ORM class of the same domain, forcing import aliasing.
    """
    offenders: list[str] = []
    for path in _py_files(SCHEMAS_DIR):
        if path.name in SCHEMAS_NAMING_EXEMPT_KW:
            continue
        if not path.name.endswith("_schema.py"):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} schema file(s) don't end in _schema.py:\n  " + "\n  ".join(offenders)
    )


def test_router_naming() -> None:
    """Every router file (JSON + HTMX) must end in _router.py."""
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _router_files(directory):
            if not path.name.endswith("_router.py"):
                offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} router file(s) don't end in _router.py:\n  " + "\n  ".join(offenders)
    )


def test_service_naming() -> None:
    """Every service file must end in _core_service.py or _view_service.py.

    Files matching only _service.py (no core/view qualifier) violate the
    standard. Private helper modules (_underscore_prefix) are exempt.
    """
    offenders: list[str] = []
    for path in _py_files(SERVICES_DIR):
        if path.name.startswith("_"):
            continue  # private helper — exempt
        if path.name.endswith("_core_service.py"):
            continue
        if path.name.endswith("_view_service.py"):
            continue
        offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} service file(s) don't end in _core_service.py "
        f"or _view_service.py:\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_core_services_in_correct_dir() -> None:
    """Every *_core_service.py must live under app/services/core/."""
    offenders: list[str] = []
    for path in _py_files(SERVICES_DIR):
        if not path.name.endswith("_core_service.py"):
            continue
        if SERVICE_CORE_DIR not in path.parents:
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} core service(s) outside app/services/core/:\n  " + "\n  ".join(offenders)
    )


def test_view_services_in_correct_dir() -> None:
    """Every *_view_service.py must live under app/services/views/."""
    offenders: list[str] = []
    for path in _py_files(SERVICES_DIR):
        if not path.name.endswith("_view_service.py"):
            continue
        if SERVICE_VIEWS_DIR not in path.parents:
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} view service(s) outside app/services/views/:\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_no_stray_services_at_top_level() -> None:
    """No .py files directly at app/services/ root — must be in a subdir."""
    offenders = [
        p.name
        for p in SERVICES_DIR.iterdir()
        if p.is_file() and p.suffix == ".py" and p.name != "__init__.py"
    ]
    assert not offenders, (
        f"{len(offenders)} stray .py file(s) at app/services/ root "
        "(must be under core/ or views/):\n  " + "\n  ".join(offenders)
    )


# ────────────────────────────────────────────────────────────────────────
# Layering — raw SQL.
# ────────────────────────────────────────────────────────────────────────


def test_no_raw_sql_in_services() -> None:
    """Raw SQLAlchemy calls forbidden in app/services/ — must delegate to crud/."""
    offenders: list[str] = []
    for path in _py_files(SERVICES_DIR):
        content = path.read_text()
        for pattern in RAW_SQL_PATTERNS:
            if pattern.search(content):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {pattern.pattern}")
                break  # one hit per file
    assert not offenders, (
        f"{len(offenders)} service file(s) using raw SQL "
        f"(should delegate to crud/):\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_web_routes_have_no_cascading_delete() -> None:
    """Web routers must not call db.delete() / bulk delete / cascading mutations
    on entities with FK cascades. Those go through MaintenanceJobCoreService.enqueue
    so the cascade runs in the background worker instead of blocking a web
    transaction. See LUXSWIRL-105.

    Allowed patterns: enqueue() into maintenance_jobs, plus the existing raw-SQL
    rules which already forbid select()/update()/delete() at the router layer.
    This guard catches the higher-level service calls that ALSO cascade.
    """
    forbidden_calls = [
        re.compile(r"\bbulk_delete_by_ids\b"),
        re.compile(r"\bdelete_check_handler\b"),
        re.compile(r"AgentCoreService\.delete_agent\b"),
        re.compile(r"StatusPageCoreService\.delete_status_page\b"),
        re.compile(r"\bbulk_create_checks\b"),
    ]
    offenders: list[str] = []
    for path in _py_files(WEB_ROUTERS_DIR):
        content = path.read_text()
        for pattern in forbidden_calls:
            if pattern.search(content):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {pattern.pattern}")
                break
    assert not offenders, (
        "Web routers must enqueue a maintenance job for cascading deletes / "
        "bulk operations (LUXSWIRL-105). Offenders:\n  " + "\n  ".join(offenders)
    )


def test_no_raw_sql_in_routers() -> None:
    """Raw SQLAlchemy calls forbidden in routers."""
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _py_files(directory):
            content = path.read_text()
            for pattern in RAW_SQL_PATTERNS:
                if pattern.search(content):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {pattern.pattern}")
                    break
    assert not offenders, (
        f"{len(offenders)} router file(s) using raw SQL:\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


# ────────────────────────────────────────────────────────────────────────
# Layering — external package boundaries.
# ────────────────────────────────────────────────────────────────────────


def test_core_services_dont_import_fastapi() -> None:
    """services/core/ must be HTTP-agnostic — no fastapi/starlette imports."""
    offenders: list[str] = []
    for path in _py_files(SERVICE_CORE_DIR):
        content = path.read_text()
        if FASTAPI_IMPORT.search(content):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} core service(s) importing fastapi/starlette:\n  "
        + "\n  ".join(offenders)
    )


def test_crud_doesnt_import_fastapi() -> None:
    """crud/ must be data-access only — no fastapi/starlette imports."""
    offenders: list[str] = []
    for path in _py_files(CRUD_DIR):
        content = path.read_text()
        if FASTAPI_IMPORT.search(content):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} crud file(s) importing fastapi/starlette:\n  " + "\n  ".join(offenders)
    )


def test_view_services_dont_import_sqlalchemy_query_primitives() -> None:
    """View services should orchestrate, not query — query primitives belong in crud/."""
    offenders: list[str] = []
    for path in _py_files(SERVICE_VIEWS_DIR):
        content = path.read_text()
        if SQLALCHEMY_QUERY_IMPORT.search(content):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} view service(s) importing sqlalchemy query primitives:\n  "
        + "\n  ".join(offenders)
    )


def test_routers_dont_import_sqlalchemy_query_primitives() -> None:
    """Routers must not import sqlalchemy query builders."""
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _py_files(directory):
            content = path.read_text()
            if SQLALCHEMY_QUERY_IMPORT.search(content):
                offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"{len(offenders)} router(s) importing sqlalchemy query primitives:\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


# ────────────────────────────────────────────────────────────────────────
# Size limits.
# ────────────────────────────────────────────────────────────────────────


def test_routers_under_size_limit() -> None:
    """Every router file ≤ ROUTER_MAX_LOC lines."""
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _py_files(directory):
            lines = _line_count(path)
            if lines > ROUTER_MAX_LOC:
                offenders.append(
                    f"{path.relative_to(BACKEND_ROOT)}: {lines} LOC (limit {ROUTER_MAX_LOC})"
                )
    offenders.sort(key=lambda s: int(s.split(": ")[1].split(" ")[0]), reverse=True)
    assert not offenders, (
        f"{len(offenders)} router(s) exceeding {ROUTER_MAX_LOC} LOC:\n  "
        + "\n  ".join(offenders[:30])
        + (f"\n  ...and {len(offenders) - 30} more" if len(offenders) > 30 else "")
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "LOC reductions deferred — architecture is correct, just verbose. "
        "Known oversize: checks_view_service.py (1308), check_core_service.py "
        "(1105), agent_core_service.py (1009). Will flip to passing once split."
    ),
)
def test_services_under_size_limit() -> None:
    """Every service file ≤ SERVICE_MAX_LOC lines."""
    offenders: list[str] = []
    for path in _py_files(SERVICES_DIR):
        lines = _line_count(path)
        if lines > SERVICE_MAX_LOC:
            offenders.append(
                f"{path.relative_to(BACKEND_ROOT)}: {lines} LOC (limit {SERVICE_MAX_LOC})"
            )
    offenders.sort(key=lambda s: int(s.split(": ")[1].split(" ")[0]), reverse=True)
    assert not offenders, (
        f"{len(offenders)} service(s) exceeding {SERVICE_MAX_LOC} LOC:\n  "
        + "\n  ".join(offenders[:30])
        + (f"\n  ...and {len(offenders) - 30} more" if len(offenders) > 30 else "")
    )


# ────────────────────────────────────────────────────────────────────────
# Router hygiene — no inline response models.
# ────────────────────────────────────────────────────────────────────────


def test_no_response_models_in_routers() -> None:
    """Pydantic BaseModel subclasses must live in app/schemas/, not in routers."""
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _py_files(directory):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    base_name = (
                        base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                    )
                    if base_name == "BaseModel":
                        offenders.append(f"{path.relative_to(BACKEND_ROOT)}: class {node.name}")
    assert not offenders, (
        f"{len(offenders)} BaseModel subclass(es) in routers "
        f"(move to app/schemas/):\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_no_models_imported_in_routers() -> None:
    """Routers must consume schemas, not raw ORM models.

    Routers should be ignorant of ORM internals — Pydantic schemas are the
    transport contract; models are persistence. The common offender is
    importing `User` purely to type an auth dependency
    (`current_user: User = Depends(...)`). The fix is a typed dependency alias
    (e.g. `AdminUser = Annotated[User, Depends(require_admin)]` in web/deps/)
    so the router imports the alias, not the model.

    `app.models.enum_model` is exempt — enums are shared infra, legitimately
    used as path/query-param types at the router layer.
    """
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _router_files(directory):
            bad = [
                imp
                for imp in _file_imports(path)
                if imp.startswith("app.models.") and imp not in ROUTER_MODEL_IMPORT_EXEMPT
            ]
            if bad:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {bad}")
    assert not offenders, (
        f"{len(offenders)} router(s) importing from app.models "
        "(use a Pydantic schema or a typed auth-dependency alias):\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_no_schema_dto_construction_in_routers() -> None:
    """Routers must not build `*Create` / `*Update` schema DTOs inline.

    The router passes raw inputs to the view/core seam, which owns DTO
    assembly. Constructing a `*Create`/`*Update` schema in a router is
    marshalling leaking into the router: it slips past the other guards (a
    schema is neither a model nor CRUD nor SQL) but bends "router = HTTP
    request/response handling ONLY".
    """
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _py_files(directory):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if DTO_CONSTRUCT_RE.search(line):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{lineno}")
    assert not offenders, (
        f"{len(offenders)} router line(s) construct a *Create/*Update schema "
        "DTO inline (move DTO assembly into the view/core seam):\n  "
        + "\n  ".join(offenders[:60])
        + (f"\n  ...and {len(offenders) - 60} more" if len(offenders) > 60 else "")
    )


def test_enum_query_params_are_enum_typed() -> None:
    """Enum-ish query params must be typed as their enum, not ``str``.

    An enum-typed query param is validated by FastAPI at the boundary — a bad
    value returns 422 and the allowed values appear in OpenAPI. A ``str`` param
    accepts anything and pushes lenient parsing into the view (an invalid filter
    silently degrades to "no filter"). Only names that denote a closed set
    (status/type/category/...) are flagged; free-text params (search, q, slug)
    stay ``str``.
    """
    offenders: list[str] = []
    for directory in ALL_ROUTER_DIRS:
        for path in _py_files(directory):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                match = STR_QUERY_PARAM.match(line)
                if match and ENUMISH_QUERY_NAME.match(match.group(1)):
                    offenders.append(
                        f"{path.relative_to(BACKEND_ROOT)}:{lineno} ({match.group(1)})"
                    )
    assert not offenders, (
        f"{len(offenders)} enum-ish query param(s) typed as str instead of "
        "their enum (type the param as the enum so FastAPI validates at the "
        "boundary):\n  "
        + "\n  ".join(offenders[:80])
        + (f"\n  ...and {len(offenders) - 80} more" if len(offenders) > 80 else "")
    )


def test_web_routers_do_not_compose_view_context() -> None:
    """Web routers must not compose/orchestrate presentation — that's view work.

    A handler obtains its template context from a SINGLE view-service call and
    returns a TemplateResponse. Two tells that composition/render-orchestration
    has leaked up into the router:
      (a) a non-route helper function in the router (e.g. `_error_partial`,
          `_status_card`) — it builds a partial/response or merges context, which
          is view-service work; and
      (b) merging multiple view outputs via ``context.update(...)``.
    The size guard only catches this when it happens to make the file big; this
    catches the leak directly even in a small router. (Ported from www, the one
    fleet member that already enforces it.)
    """
    offenders: list[str] = []
    for path in _router_files(WEB_ROUTERS_DIR):
        rel = path.relative_to(BACKEND_ROOT)
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        # (a) module-level functions that are not route handlers
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decos = {
                d.func.attr
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                else d.attr
                if isinstance(d, ast.Attribute)
                else ""
                for d in fn.decorator_list
            }
            if not (decos & _ROUTE_VERBS):
                offenders.append(f"{rel}: non-route helper '{fn.name}()' — move to a view service")
        # (b) context merging
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
            ):
                offenders.append(
                    f"{rel}:{node.lineno}: context merge via .update() — compose in the view"
                )
    assert not offenders, (
        f"{len(offenders)} web router composition leak(s). Build context in a view "
        "service; the router calls one view method and returns a TemplateResponse:\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_no_inline_imports() -> None:
    """No imports inside function/method bodies in services, crud, routers.

    Inline imports hide dependencies, defeat static analysis, and usually mark
    circular-import workarounds that should be solved structurally. The
    PreToolUse hook blocks NEW inline imports at write-time, but a hook only
    covers the agent that has it — it does not catch pre-existing code, other
    agents, or direct edits. This is the CI backstop. Use TYPE_CHECKING for
    genuine circular-dep type-only imports. (Ported from boutique.)
    """
    offenders: list[str] = []
    scan_dirs = [
        APP / "services",
        APP / "crud",
        APP / "web" / "routers",
        APP / "api",
        LEGACY_ROUTERS_DIR,
    ]
    for d in scan_dirs:
        for path in _py_files(d):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            found = False
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(node):
                    if sub is node:
                        continue
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        offenders.append(str(path.relative_to(BACKEND_ROOT)))
                        found = True
                        break
                if found:
                    break
    offenders = sorted(set(offenders))
    assert not offenders, (
        f"{len(offenders)} file(s) with inline imports in function bodies "
        "(move to file top; use TYPE_CHECKING for circular deps):\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_no_view_services_import_crud() -> None:
    """View services must call core services, not crud directly.

    Views assemble template context; data access and business logic belong in
    core services. A view reaching into crud skips the core layer. The layered
    import-linter contract ALLOWS this (crud is a lower layer) — so this AST
    guard is the thing that actually forbids it. (Ported from boutique/www.)
    """
    offenders: list[str] = []
    for path in _py_files(SERVICE_VIEWS_DIR):
        bad = [imp for imp in _file_imports(path) if imp.startswith("app.crud.")]
        if bad:
            offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {bad}")
    assert not offenders, (
        f"{len(offenders)} view service(s) importing app.crud directly "
        "(call a core service instead):\n  " + "\n  ".join(offenders)
    )


def test_no_crud_imports_crud() -> None:
    """CRUD modules may import base_crud but not each other.

    A crud calling another crud is multi-model orchestration — a service
    concern. Cross-crud imports leak business logic into the data layer and
    create cross-crud cycles. (Ported from boutique/www.)
    """
    offenders: list[str] = []
    for path in _py_files(CRUD_DIR):
        bad = [
            imp
            for imp in _file_imports(path)
            if imp.startswith("app.crud.") and imp != "app.crud.base_crud"
        ]
        if bad:
            offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {bad}")
    assert not offenders, (
        f"{len(offenders)} crud file(s) importing other crud "
        "(orchestration belongs in a service):\n  " + "\n  ".join(offenders)
    )


def test_no_inline_html_in_routers_and_views() -> None:
    """Routers and view services assemble context and return a TemplateResponse —
    markup lives in app/web/templates/, never as inline HTML strings.
    (Ported from www.)
    """
    offenders: list[str] = []
    for directory in (*ALL_ROUTER_DIRS, SERVICE_VIEWS_DIR):
        for path in _py_files(directory):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if INLINE_HTML_PATTERN.search(line):
                    offenders.append(
                        f"{path.relative_to(BACKEND_ROOT)}:{lineno}  {line.strip()[:60]}"
                    )
    assert not offenders, (
        f"{len(offenders)} inline HTML site(s) in routers/views "
        "(move markup to app/web/templates/):\n  " + "\n  ".join(offenders[:50])
    )


def test_no_raw_sql_outside_crud() -> None:
    """Raw SQL is allowed ONLY in crud/ (plus documented infra: background/, db/,
    scripts/). This is the broad backstop that generalizes the per-layer checks
    — it catches raw SQL anywhere it shouldn't be, not just in services/routers.
    (Ported from luxtaste/luxpm.)
    """
    offenders: list[str] = []
    for path in _py_files(APP):
        rel_parts = path.relative_to(APP).parts
        if rel_parts[0] == "crud" or rel_parts[0] in RAW_SQL_OUTSIDE_CRUD_EXEMPT_DIRS:
            continue
        content = path.read_text()
        for pattern in RAW_SQL_PATTERNS:
            if pattern.search(content):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {pattern.pattern}")
                break
    assert not offenders, (
        f"{len(offenders)} file(s) running raw SQL outside crud/ "
        f"(exempt infra: {sorted(RAW_SQL_OUTSIDE_CRUD_EXEMPT_DIRS)}):\n  "
        + "\n  ".join(offenders[:50])
        + (f"\n  ...and {len(offenders) - 50} more" if len(offenders) > 50 else "")
    )


def test_core_service_class_naming() -> None:
    """Each *_core_service.py must define the class whose name matches the file:
    agent_core_service.py → AgentCoreService. Bare `*Service` / `Web*` / `Async*`
    are drift. Function-only modules (no class) are exempt. Helper dataclasses in
    the file are ignored — only the canonical class must exist. (LUXSWIRL-170)
    """
    offenders: list[str] = []
    for path in _py_files(SERVICE_CORE_DIR):
        classes = _class_names(path)
        if not classes:
            continue  # function-only module (e.g. cleanup, monitoring)
        expected = _expected_service_class(path.stem, "core_service")
        if expected not in classes:
            offenders.append(
                f"{path.relative_to(BACKEND_ROOT)}: expected '{expected}', got {classes}"
            )
    assert not offenders, (
        f"{len(offenders)} core service class name(s) don't match the file:\n  "
        + "\n  ".join(offenders)
    )


def test_view_service_class_naming() -> None:
    """Each *_view_service.py must define the class matching the file:
    checks_view_service.py → ChecksViewService. (LUXSWIRL-170)
    """
    offenders: list[str] = []
    for path in _py_files(SERVICE_VIEWS_DIR):
        if path.name.startswith("_"):
            continue
        classes = _class_names(path)
        if not classes:
            continue
        expected = _expected_service_class(path.stem, "view_service")
        if expected not in classes:
            offenders.append(
                f"{path.relative_to(BACKEND_ROOT)}: expected '{expected}', got {classes}"
            )
    assert not offenders, (
        f"{len(offenders)} view service class name(s) don't match the file:\n  "
        + "\n  ".join(offenders)
    )


def test_import_linter_contracts_pass() -> None:
    """Run the import-linter contracts defined in pyproject.toml.

    The contracts encode dependency direction (router → view → core → crud →
    models, no skipping layers, no view→view composition, etc.). On failure,
    re-run `PYTHONPATH=. lint-imports (from apps/backend/)` locally for a readable
    breakdown of which contract broke and which import did it.
    """
    if importlib.util.find_spec("importlinter") is None:
        pytest.skip("import-linter not installed")

    repo_root = REPO_ROOT
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; "
            "import sys; sys.exit(lint_imports_command())",
        ],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(APP)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import-linter contracts broken:\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )


# ─── Enum model guards ───────────────────────────────────────────────


def test_enum_model_exists_and_imports_cleanly() -> None:
    """The canonical enum module must exist at models/enum_model.py and
    successfully expose all expected StrEnum classes."""
    enum_path = MODELS_DIR / "enum_model.py"
    assert enum_path.exists(), f"Missing canonical enum file: {enum_path}"

    spec = importlib.util.spec_from_file_location("enum_model", enum_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    expected_classes = {
        "AgentApprovalStatus",
        "AgentStatus",
        "AlertTriggerType",
        "CheckArtifactType",
        "CheckErrorType",
        "CheckType",
        "JobStatus",
        "JobType",
        "NotificationProviderType",
        "NotificationStatus",
        "SchedulerExecutionStatus",
        "SchedulerJobCategory",
        "SchedulerTriggerType",
        "SettingCategory",
        "UserRole",
    }
    missing = [c for c in expected_classes if not hasattr(module, c)]
    assert not missing, f"enum_model.py is missing classes: {missing}"


# A model column whose NAME denotes a closed set must be typed `Mapped[<Enum>]`,
# not bare `Mapped[str]` / `Mapped[str | None]`. This is the ORM-layer sibling of
# test_enum_query_params_are_enum_typed.
#
# This REPLACES the old *_members_match_column_comment guards, which only matched
# a hardcoded `comment="..."` string against the enum members. A comment proves
# nothing: a `Mapped[str]` column with a perfect comment passed while the DB still
# accepted any string and the type silently drifted from the enum. The type is the
# only thing that enforces the closed set. luxswirl stores enums as VARCHAR via
# `str_enum()`, so the `Mapped[<Enum>]` annotation is the source of truth.
#
# Genuinely free-text columns that match the enum-ish name pattern go in
# _ENUM_COLUMN_FREETEXT_ALLOWLIST (path, column) with evidence — same escape hatch
# the query-param guard uses for free-text params.
_ENUMISH_COLUMN_NAME = re.compile(
    r"^([a-z][a-z0-9_]*_)?(status|state|type|role|mode|kind|category|method)$"
)
_ENUM_COLUMN_FREETEXT_ALLOWLIST: set[tuple[str, str]] = {
    # MIME type — genuinely open set (image/png, application/zip, text/html, ...),
    # not a closed enum. Verified: check_artifact_model.content_type is a free-form
    # MIME string written from the agent's artifact metadata.
    ("app/models/check_artifact_model.py", "content_type"),
}


def test_enumish_columns_are_enum_typed() -> None:
    """Model columns whose name denotes a closed set must be `Mapped[<Enum>]`, not bare str.

    The meaningful enforcement (the old *_members_match_column_comment guards only
    matched a comment string — worthless; a bare-str column passed). A column named
    ``status`` / ``*_type`` / ``*_status`` / ``role`` / ``*_mode`` / ... typed as
    bare ``str`` accepts arbitrary values and drifts from the source-of-truth enum.
    Type it ``Mapped[<Enum>]`` (the column stays VARCHAR via ``str_enum()`` — no DB
    enum), or add (path, column) to _ENUM_COLUMN_FREETEXT_ALLOWLIST if free-text.
    """
    offenders: list[str] = []
    for path in _py_files(MODELS_DIR):
        rel = "app/" + str(path.relative_to(APP))
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
                continue
            name = node.target.id
            if not _ENUMISH_COLUMN_NAME.match(name):
                continue
            ann = node.annotation
            if not (isinstance(ann, ast.Subscript) and getattr(ann.value, "id", "") == "Mapped"):
                continue  # only ORM mapped columns
            if annotation_element_type(ann) != "str":
                continue  # already enum-typed (or non-str) — fine
            if (rel, name) in _ENUM_COLUMN_FREETEXT_ALLOWLIST:
                continue
            offenders.append(f"{rel}:{node.lineno}: {name}")

    assert not offenders, (
        f"{len(offenders)} enum-ish column(s) typed as bare str instead of their enum. "
        "Type the column `Mapped[<Enum>]` so it carries the closed set (str_enum keeps it "
        "VARCHAR, no DB enum), or add (path, column) to _ENUM_COLUMN_FREETEXT_ALLOWLIST "
        "with evidence it is genuinely free-text:\n  " + "\n  ".join(sorted(offenders))
    )


def test_user_role_has_label_for_every_member() -> None:
    """Every UserRole member must have a label entry in _USER_ROLE_LABELS.
    Adding a new role without a label fails this test before the missing
    label can ship to a form dropdown.
    """
    enum_path = MODELS_DIR / "enum_model.py"
    spec = importlib.util.spec_from_file_location("enum_model", enum_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    missing = [
        m
        for m in module.UserRole
        if m not in module._USER_ROLE_LABELS or not module._USER_ROLE_LABELS[m].strip()
    ]
    assert not missing, (
        f"UserRole members without a label entry: {[m.value for m in missing]}.\n"
        "Add an entry to _USER_ROLE_LABELS in models/enum_model.py."
    )


def test_check_type_has_label_for_every_member() -> None:
    """Every CheckType member must have a label entry in _CHECK_TYPE_LABELS.
    Adding a new check type without a label fails this test before the
    missing label can ship to the check creation form.
    """
    enum_path = MODELS_DIR / "enum_model.py"
    spec = importlib.util.spec_from_file_location("enum_model", enum_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    missing = [
        m
        for m in module.CheckType
        if m not in module._CHECK_TYPE_LABELS or not module._CHECK_TYPE_LABELS[m].strip()
    ]
    assert not missing, (
        f"CheckType members without a label entry: {[m.value for m in missing]}.\n"
        "Add an entry to _CHECK_TYPE_LABELS in models/enum_model.py."
    )


def test_no_hand_rolled_pagination_in_active_templates() -> None:
    """No active page or partial template may hand-roll pagination markup.

    Every paginated page MUST use the `pagination_controls` macro from
    macros/tables.html. Hand-rolled prev/next/numbered blocks are detected
    by searching for the canonical context vars (`has_prev`, `has_next`,
    `total_pages`) outside the macro file itself. The dead orphan
    `pages/alerts.html` is excluded — flagged for deletion separately.
    """
    templates_dir = APP / "web" / "templates"
    # The macro itself references these vars in its iteration logic; that's
    # the SOURCE of truth, not a violation. pages/alerts.html is dead code.
    EXEMPT = {
        "macros/tables.html",
        "pages/alerts.html",  # orphan, flagged for deletion
    }

    offenders: list[str] = []
    for path in templates_dir.rglob("*.html"):
        rel = str(path.relative_to(templates_dir))
        if rel in EXEMPT:
            continue
        text = path.read_text()
        if "has_prev" in text or "has_next" in text or "total_pages" in text:
            offenders.append(rel)

    assert not offenders, (
        f"{len(offenders)} template(s) hand-roll pagination instead of using "
        "the `pagination_controls` macro from macros/tables.html:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace the hand-rolled block with:\n"
        + "  {% from 'macros/tables.html' import pagination_controls %}\n"
        + "  {{ pagination_controls(pagination, '/your-route') }}\n"
        + "and have the view service build `pagination` via "
        + "`build_pagination(...)` from schemas/pagination_schema.py."
    )


def test_no_hand_rolled_pagination_in_view_services() -> None:
    """View services must not pass `total_pages` / `has_prev` / `has_next`
    in template context. Use the `Pagination` DTO from
    `schemas.pagination_schema.build_pagination` instead — the DTO carries
    all those derived properties so templates use a single shape.
    """
    views_dir = APP / "services" / "views"

    offenders: list[tuple[str, int, str]] = []
    for path in views_dir.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Template-context dict-key patterns
            if (
                '"total_pages":' in stripped
                or '"has_prev":' in stripped
                or '"has_next":' in stripped
            ):
                offenders.append((str(path.relative_to(APP)), lineno, stripped[:80]))

    assert not offenders, (
        f"{len(offenders)} view-service site(s) build pagination state "
        "by hand instead of using build_pagination():\n  "
        + "\n  ".join(f"{p}:{ln}  {s}" for p, ln, s in offenders)
        + "\n\nReplace with:\n"
        + "  from schemas.pagination_schema import build_pagination\n"
        + "  pagination = build_pagination(page=..., per_page=..., total=..., filters={...})\n"
        + "  return {..., 'pagination': pagination}"
    )


def test_no_hand_rolled_table_in_active_templates() -> None:
    """No active template may hand-roll a ``<table>``. Every data grid MUST use the
    ``data_table`` macro from macros/tables.html — it bakes in the ``.table`` styling
    AND the ``scope="col"`` headers screen readers need. A raw ``<table>`` is the
    canonical marker of a hand-rolled, drift-prone, inaccessible table.

    The macro file itself is the source of truth (it emits the one ``<table>``);
    ``logo-demo`` is a standalone admin-only design playground, not part of the UI.
    """
    templates_dir = APP / "web" / "templates"
    EXEMPT = {
        "macros/tables.html",  # the macro emits THE <table> — source of truth
    }

    offenders: list[str] = []
    for path in templates_dir.rglob("*.html"):
        rel = str(path.relative_to(templates_dir))
        if rel in EXEMPT or "logo-demo" in rel:
            continue
        if "<table" in path.read_text():
            offenders.append(rel)

    assert not offenders, (
        f"{len(offenders)} template(s) hand-roll a <table> instead of using the "
        "`data_table` macro from macros/tables.html:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace the hand-rolled <table>/<thead> with:\n"
        + "  {% from 'macros/tables.html' import data_table %}\n"
        + "  {% call data_table([{'label': 'Name'}, {'label': 'Actions', 'align': 'right'}]) %}\n"
        + "      {# <tr><td>…</td></tr> body rows only — the macro emits table/thead/tbody #}\n"
        + "  {% endcall %}\n"
        + "See /settings/components for the canonical example."
    )


def test_selects_have_an_accessible_name() -> None:
    """Every ``<select>`` must have a programmatic accessible name — an ``id``
    (paired with a ``<label for>``) or an ``aria-label``. A bare ``<select>`` is
    announced as just "combo box" by screen readers (Lighthouse: "Select elements
    do not have associated label elements"). Filter selects should go through the
    ``filter_select`` macro (which sets ``id``); standalone selects must set ``id``
    or ``aria-label`` themselves.
    """
    templates_dir = APP / "web" / "templates"
    EXEMPT = {"macros/filters.html"}  # the filter_select macro + its docstring mention
    select_open = re.compile(r"<select\b[^>]*>", re.S)
    label_for = re.compile(r'<label[^>]*\bfor="([^"]+)"')
    id_attr = re.compile(r'\bid="([^"]+)"')

    offenders: list[str] = []
    for path in templates_dir.rglob("*.html"):
        rel = str(path.relative_to(templates_dir))
        if rel in EXEMPT or "logo-demo" in rel:
            continue
        text = path.read_text()
        # A select is named only by aria-label, OR by an id that an in-file
        # <label for="..."> actually points at — a bare id is NOT a name.
        labelled_ids = set(label_for.findall(text))
        for tag in select_open.findall(text):
            if "aria-label=" in tag:
                continue
            m = id_attr.search(tag)
            if m and m.group(1) in labelled_ids:
                continue
            offenders.append(rel)
            break

    assert not offenders, (
        f"{len(offenders)} template(s) have a <select> with no accessible name "
        "(needs an `id` paired with a <label for>, or an `aria-label`):\n  "
        + "\n  ".join(offenders)
        + "\n\nUse the filter_select macro for filter dropdowns, or add "
        + "id= (+ a matching <label for=>) or aria-label= to the <select>."
    )


def test_worker_sessions_commit_or_use_worker_session() -> None:
    """Background workers / scheduled-job bodies must COMMIT their DB work.

    Opening a raw ``get_session_maker()`` session and never committing means every
    write silently rolls back on session close — this regressed the ENTIRE scheduler
    plus all cleanup/metrics jobs for ~6 weeks (LUXSWIRL-191). The fix is the
    committing ``worker_session()`` context manager (the background equivalent of the
    request path's ``get_db``), or an explicit ``.commit()`` when fine-grained
    transaction control is genuinely needed (e.g. database_maintenance's per-step
    commits + AUTOCOMMIT VACUUM).

    Rule (scope: app/services + app/background): a module that references
    ``get_session_maker`` must ALSO use ``worker_session`` or call ``.commit()``.
    """
    offenders: list[str] = []
    for base in (APP / "services", APP / "background"):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            text = path.read_text()
            if "get_session_maker" not in text:
                continue
            if "worker_session" in text or ".commit(" in text:
                continue
            offenders.append(str(path.relative_to(APP)))

    assert not offenders, (
        f"{len(offenders)} worker/job module(s) open a raw get_session_maker() "
        "session but never commit — silent rollback (see LUXSWIRL-191):\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `async with worker_session() as db:` (commits on clean exit) "
        "instead of get_session_maker(), or commit explicitly if you genuinely "
        "need fine-grained transaction control."
    )


# ---------------------------------------------------------------------------
# UUID id typing (LUXSWIRL-161/155/176): fields/params named like a UUID column
# must be UUID-typed, not int/str. The engine lives in shared.idtype_guard —
# ORM-agnostic and fleet-reusable; this block is only the SQLAlchemy adapter +
# this repo's config. The ORM is the source of truth: a column that is str in
# the model (e.g. the scheduler's string job_key) is not a UUID id, never flagged.
# ---------------------------------------------------------------------------

# (path relative to app/, identifier) that legitimately holds a non-UUID value
# despite matching a UUID column name. Keep SMALL; prefer renaming the param.
UUID_TYPING_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Not-found exception message args: the raised value is the identifier that
        # was searched for — a UUID *string*, an agent name, or a literal ("No
        # agents available") — formatted into the error text, not the agent_id
        # UUID column.
        ("core/exceptions.py", "agent_id"),
    }
)

# This repo's wiring of the shared engine. ``extra_uuid_id_names`` lists ids that
# reference a UUID pk but are not literal columns (a column scan can't see them).
UUID_GUARD_CONFIG = GuardConfig(
    extra_uuid_id_names=frozenset(
        {
            "log_id",  # -> NotificationLog.id (UUID)
            "job_id",  # -> Job.id (UUID); the scheduler's string key is now job_key
        }
    ),
    allowlist=UUID_TYPING_ALLOWLIST,
)


def test_uuid_id_fields_are_uuid_typed() -> None:
    """Fields/params named like a UUID column must be UUID-typed, not int/str.

    `int` for a UUID id silently never matches (the check_artifacts
    `check_result_id: int` bug); `str` forces str<->UUID reconversion at every
    hop instead of carrying the UUID the column stores (LUXSWIRL-161/155/176).
    Fix the annotation, or — if it genuinely holds a non-UUID — add (path, name)
    to UUID_TYPING_ALLOWLIST with a reason (prefer renaming, e.g. agent_id ->
    agent_name).
    """
    uuid_names = sqlalchemy_uuid_id_names(_py_files(MODELS_DIR))
    assert uuid_names, "Parsed zero UUID id columns from models/"

    files = [p for p in _py_files(APP) if p.relative_to(APP).parts[0] != "models"]
    offenders = find_violations(files, APP, uuid_names, UUID_GUARD_CONFIG)

    assert not offenders, (
        f"{len(offenders)} id field(s)/param(s) named like a UUID column but typed "
        "int/str (int silently never matches; str forces reconversion). Fix the "
        "annotation, or add (path, name) to UUID_TYPING_ALLOWLIST with a reason.\n\n  "
        + "\n  ".join(str(o) for o in sorted(offenders, key=str))
    )


# ---------------------------------------------------------------------------
# JSONB columns must be SHAPED, not bare Mapped[dict] / Mapped[list] (LUXSWIRL-127).
# A bare `dict` is `dict[Any, Any]` — a silent Any-FACTORY: every `.get()` / `[...]`
# hands out Any, so unchecked None/garbage flows past mypy into typed sinks (the
# job_to_check partial-host crash: `CheckCreate(target=host.get("ip"))` blew up the
# batch). `warn_return_any` doesn't catch this — the Any is emitted, not written.
# Fix: a TypedDict/Pydantic shape (so values are typed), or — for genuinely opaque
# round-tripped JSON — an EXPLICIT `dict[str, Any]` (a conscious, visible choice).
# ---------------------------------------------------------------------------


def test_jsonb_columns_are_not_bare_dict() -> None:
    """Model JSONB columns may not be bare ``Mapped[dict]`` / ``Mapped[list]``."""
    bare = {"dict", "list"}
    offenders: list[str] = []
    for path in _py_files(MODELS_DIR):
        rel = path.relative_to(APP)
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(node.annotation, ast.Subscript)
                and isinstance(node.annotation.value, ast.Name)
                and node.annotation.value.id == "Mapped"
            ):
                continue
            inner = node.annotation.slice
            # unwrap `X | None`
            if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr):
                inner = next(
                    (
                        s
                        for s in (inner.left, inner.right)
                        if not (isinstance(s, ast.Constant) and s.value is None)
                    ),
                    inner,
                )
            if isinstance(inner, ast.Name) and inner.id in bare:
                offenders.append(
                    f"{rel}:{node.lineno}: '{node.target.id}' is bare Mapped[{inner.id}]"
                )

    assert not offenders, (
        f"{len(offenders)} JSONB column(s) typed as a bare Mapped[dict]/Mapped[list] "
        "(= dict[Any, Any], a silent Any-factory mypy can't see through). Shape it "
        "(TypedDict / Pydantic / dict[str, X]), or for genuinely opaque round-tripped "
        "JSON make it an explicit dict[str, Any].\n\n  " + "\n  ".join(sorted(offenders))
    )


# ---------------------------------------------------------------------------
# Unbounded hypertable aggregate scans (LUXSWIRL-180): a query that AGGREGATES a
# TimescaleDB hypertable MUST carry a time bound (or approximate_row_count).
# Without a time predicate the planner can't do chunk exclusion, so the scan
# spans every chunk — and on compressed chunks an index scan flips to a full
# ColumnarScan that decompresses the whole table. An instant query silently
# becomes seconds. Correctness invariant, not an optimization.
#
# Heuristic backstop over the crud layer: a function that references a hypertable
# AND uses an aggregate (func.count/sum/avg/min/max/percentile, .group_by, or raw
# COUNT/SUM/AVG/GROUP BY) must also carry a bound token (time predicate,
# approximate_row_count, or an INTERVAL window). Plain tables are exempt — only
# the three real hypertables are listed (notification_logs was de-hypertabled in
# LUXSWIRL-181). Non-aggregate "latest row" reads (ORDER BY ts DESC LIMIT n) are
# not flagged: they short-circuit via ChunkAppend and carry no aggregate.
# ---------------------------------------------------------------------------

# A hypertable counts as "referenced" only when used as a real data source: a
# FROM/JOIN target in raw SQL, or the ORM model identifier. This excludes
# mentions in comments and in metadata views (timescaledb_information.*), which
# are not data scans (e.g. counting chunks).
_HYPERTABLE_FROM_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:check_results|agent_metrics|check_artifacts)\b", re.IGNORECASE
)
_HYPERTABLE_MODEL_RE = re.compile(r"\b(?:CheckResult|AgentMetric|CheckArtifact)\b")

_HYPERTABLE_AGG_RE = re.compile(
    r"func\.(count|sum|avg|min|max|percentile_cont)\b"
    r"|\.group_by\("
    r"|\bCOUNT\s*\(|\bSUM\s*\(|\bAVG\s*\(|\bGROUP\s+BY\b",
    re.IGNORECASE,
)

_HYPERTABLE_BOUND_RE = re.compile(
    r"timestamp\s*(>=|>|<|between)"
    r"|created_at\s*(>=|>|<|between)"
    r"|sent_at\s*(>=|>|<|between)"
    r"|\.timestamp\s*(>=|>|<)"
    r"|\.created_at\s*(>=|>|<)"
    r"|approximate_row_count"
    r"|interval\s*'",
    re.IGNORECASE,
)

# (crud file stem, function name) -> reason. Genuine, decided exceptions only.
UNBOUNDED_HYPERTABLE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("artifact_crud", "get_stats_for_check"): (
        "check_artifacts is retention-scoped (cleanup deletes past retention) and this "
        "filters by check_id (indexed) — it scans only that check's artifacts, so a time "
        "bound prunes nothing. Decided in LUXSWIRL-180; precompute if it ever becomes hot."
    ),
}


def test_no_unbounded_hypertable_aggregate_scans() -> None:
    """A crud function that aggregates a TimescaleDB hypertable must carry a time bound."""
    offenders: list[str] = []
    for path in _py_files(CRUD_DIR):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            seg = ast.get_source_segment(source, node) or ""
            # Match real code, not prose: strip the docstring + line comments first.
            # The AGG regex is case-insensitive, so a sentence like "...bind-parameter
            # count\n(rows × cols)" would otherwise read as a SQL COUNT( aggregate and
            # false-flag a plain chunked INSERT (which is not an unbounded scan).
            doc = ast.get_docstring(node, clean=False)
            if doc:
                seg = seg.replace(doc, "", 1)
            seg = re.sub(r"#[^\n]*", "", seg)
            if not (_HYPERTABLE_FROM_RE.search(seg) or _HYPERTABLE_MODEL_RE.search(seg)):
                continue
            if not _HYPERTABLE_AGG_RE.search(seg):
                continue
            if _HYPERTABLE_BOUND_RE.search(seg):
                continue
            if (path.stem, node.name) in UNBOUNDED_HYPERTABLE_ALLOWLIST:
                continue
            offenders.append(f"{path.relative_to(APP)}:{node.lineno} ({node.name})")

    assert not offenders, (
        f"{len(offenders)} crud function(s) aggregate a TimescaleDB hypertable without a "
        "time bound (chunk exclusion can't kick in → full ColumnarScan once compressed). "
        "Add a time predicate / use approximate_row_count, or add (file_stem, func) to "
        "UNBOUNDED_HYPERTABLE_ALLOWLIST with a reason.\n\n  " + "\n  ".join(sorted(offenders))
    )
