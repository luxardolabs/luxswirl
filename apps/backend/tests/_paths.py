"""Shared path resolution for tests that target source files.

Layout (post apps/ migration): tests live at ``apps/backend/tests/``; the
backend package is ``apps/backend/app/``; ``pyproject.toml`` is at
``apps/backend/``. The old host-vs-container dual-layout machinery is gone — the
baked image now mirrors the repo layout exactly, so a single resolution works
everywhere.
"""

from __future__ import annotations

from pathlib import Path

_COMPONENT_ROOT = Path(__file__).resolve().parent.parent  # apps/backend

# Source root: directory that contains `crud/`, `services/`, `models/`, etc.
BACKEND_ROOT: Path = _COMPONENT_ROOT / "app"

# Repo/component root: directory that contains `pyproject.toml`. Used as cwd by
# anything that invokes a poetry/pip-installed CLI (import-linter, etc.).
REPO_ROOT: Path = _COMPONENT_ROOT


# Directory names skipped when walking BACKEND_ROOT recursively. `alembic` and
# `reports` live at the component root (outside `app/`), so they are never
# walked; only bytecode caches need excluding.
EXCLUDED_WALK_DIRS: frozenset[str] = frozenset({"__pycache__"})


def is_backend_file(path: Path) -> bool:
    """True when `path` lives inside the backend tree (not a cache dir)."""
    return not any(part in EXCLUDED_WALK_DIRS for part in path.parts)


def repo_path(rel: str) -> Path:
    """Resolve an ``app/...``-style identifier string to a real Path.

    Hard-coded identifier strings like ``"app/web/routers/foo.py"`` appear in
    allowlists/blocklists across the test suite; they join against the component
    root.
    """
    return _COMPONENT_ROOT / rel
