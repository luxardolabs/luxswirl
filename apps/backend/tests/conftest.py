"""Backend test conftest — path setup and marker registration.

Test taxonomy (mirrors luxwx):
- `pure`        — no I/O, no DB, no network. Fast, runs in seconds.
- `integration` — real DB via the `db` fixture (transactional rollback per test).
                  Requires `compose.test.yaml` test DB up. The `db` fixture is
                  registered globally here via `pytest_plugins`; tests just take a
                  `db` parameter — no import needed.
- `api`         — full FastAPI TestClient with overridden deps.

Run a single tier:
    pytest -m pure                       # fast feedback loop
    pytest -m integration                # touches the DB
    pytest -m "not integration"          # skip DB-bound tests
"""

from __future__ import annotations

import sys
from pathlib import Path

# Layout: this file is apps/backend/tests/conftest.py.
_TESTS_DIR = Path(__file__).resolve().parent  # apps/backend/tests
_COMPONENT_ROOT = _TESTS_DIR.parent  # apps/backend  → resolves `import app.*`
_APPS_ROOT = _COMPONENT_ROOT.parent  # apps/         → resolves `import shared.*`

# Prepend in reverse priority so the highest-priority entry ends at index 0.
# `app.*` namespacing means there is no longer any agent/backend module
# collision to defend against — the agent tree is a separate component and is
# never on this path.
for candidate in (_APPS_ROOT, _COMPONENT_ROOT, _TESTS_DIR):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


# Register shared fixtures as a plugin so every test gets them by name. Without
# this, each test file does `from fixtures.db import db`, which the test method's
# own `db` parameter then shadows — tripping ruff F811 ~147× across the suite.
pytest_plugins = ["fixtures.db", "fixtures.committed_db"]


def pytest_configure(config):
    """Register the test-tier markers so `pytest --strict-markers` is happy."""
    config.addinivalue_line(
        "markers",
        "pure: pure-logic test — no I/O, no DB, no network. Default tier.",
    )
    config.addinivalue_line(
        "markers",
        "integration: requires the test DB from compose.test.yaml (uses `db` fixture).",
    )
    config.addinivalue_line(
        "markers",
        "api: full FastAPI TestClient with overridden dependencies.",
    )
