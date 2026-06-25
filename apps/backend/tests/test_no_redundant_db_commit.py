"""
Architectural lint: routers and services must not call db.commit() / db.rollback().

Per LUXSWIRL-98 — `get_db()` (the FastAPI dependency in `db/database.py`)
auto-commits on clean return and auto-rolls-back on any exception that
propagates to the dependency. Calling `db.commit()` or `db.rollback()`
inside a router or service is either:

  - redundant (get_db() will commit anyway on return), or
  - a smell (the router is swallowing an exception that should propagate
    instead, and rollback is patching over the leaked partial transaction).

Code that runs OUTSIDE the FastAPI request lifecycle (startup, background
tasks, the maintenance worker, one-shot scripts) creates its own session via
`session_maker()` / `worker_session()` and owns its transactions — those
files are listed in NON_REQUEST_FILES and exempt.

There is NO per-site allowlist and NO pending-refactor quarantine. Every
request-path router/service raises instead of swallowing, and the fleet
exception handlers in main.py render the error (content-negotiated:
JSON / HTMX toast / error page — LUXSWIRL-179). If you are tempted to add an
exemption to get this test green, fix the code instead: raise the exception
and let get_db() roll back.
"""

from __future__ import annotations

import re
from pathlib import Path

from _paths import BACKEND_ROOT as BACKEND
from _paths import is_backend_file

# Files that legitimately manage transactions outside the FastAPI request
# lifecycle. They create sessions via `session_maker()` / `worker_session()`
# (not `get_db()`), so they own their commit/rollback. This is a real
# architectural boundary (different session lifecycle), NOT a debt quarantine.
NON_REQUEST_FILES: frozenset[str] = frozenset(
    {
        "app/db/database.py",  # the get_db dependency itself
        "app/main.py",  # startup: scheduler defaults
        "app/background/job_purge.py",  # background task
        "app/background/database_maintenance.py",  # AUTOCOMMIT VACUUM connection
        "app/scripts/migrate_encrypt_checks.py",  # one-shot migration
        "app/scripts/test_create_status_page.py",  # dev seed script
        "app/scripts/create_test_status_page.py",  # dev seed script
        "app/scripts/cleanup_duplicate_internal_checks.py",  # one-shot cleanup
    }
)


COMMIT_PATTERN = re.compile(r"\bawait\s+(?:db|session)\.commit\s*\(")
ROLLBACK_PATTERN = re.compile(r"\bawait\s+(?:db|session)\.rollback\s*\(")


def _backend_files() -> list[Path]:
    """All Python files under app/, excluding non-backend siblings."""
    return [p for p in BACKEND.rglob("*.py") if is_backend_file(p.relative_to(BACKEND))]


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, kind, line) for every commit/rollback in the file."""
    sites: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if COMMIT_PATTERN.search(line):
            sites.append((lineno, "commit", line.strip()))
        elif ROLLBACK_PATTERN.search(line):
            sites.append((lineno, "rollback", line.strip()))
    return sites


def test_no_redundant_commit_or_rollback() -> None:
    """
    No `await db.commit()` or `await db.rollback()` outside NON_REQUEST_FILES.

    Routers and services go through get_db(), which owns the transaction:
    they raise and let it propagate. To declare a new transaction-owning file,
    add it to NON_REQUEST_FILES with a comment explaining why it doesn't go
    through get_db(). There is no other escape hatch by design.
    """
    violations: list[str] = []
    for path in _backend_files():
        # Identifier always rendered as `app/...` so it matches NON_REQUEST_FILES.
        rel = "app/" + str(path.relative_to(BACKEND))
        if rel in NON_REQUEST_FILES:
            continue
        for lineno, _kind, line in _scan_file(path):
            violations.append(f"{rel}:{lineno}: {line}")

    assert not violations, (
        "Routers/services must not call db.commit() or db.rollback() — "
        "the get_db() dependency in db/database.py auto-commits on clean "
        "return and auto-rolls-back on exception. Raise instead of swallowing; "
        "the main.py exception handlers render the error. To declare a "
        "transaction-owning file, add it to NON_REQUEST_FILES.\n\n"
        "Violations:\n  " + "\n  ".join(violations)
    )
