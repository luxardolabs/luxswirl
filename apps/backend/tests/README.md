# LuxSwirl Test Suite

**Tracking issue:** LUXSWIRL-127

A test platform for the LuxSwirl monitoring system. Tests are organized by
**functional domain** (not by test type) so each directory mirrors a product
area. Pure logic tests, integration tests, and API tests live side-by-side
within each domain.

---

## Tooling

| Tool | Version | Purpose |
|---|---|---|
| **pytest** | 9.0 | Test runner |
| **pytest-asyncio** | 1.3 | Async test support (LuxSwirl is fully async — FastAPI + asyncpg) |
| **pytest-cov** | 7.0 | Coverage reporting |
| **hypothesis** | 6.x | Property-based testing — generates random inputs to verify invariants (state-key stability, SSL band monotonicity, threshold-order invariance). Impossible to fabricate — they either find real counterexamples or they don't. |
| **mutmut** | 3.x | Mutation testing — changes source code one line at a time and checks if any test fails. Surviving mutant = test gap. Verifies tests have teeth, not just coverage. |
| **Faker** | 40.0 | Random fixture data (names, IPs, etc.) when factories need realistic-looking values |

---

## Test Organization

```
tests/
├── conftest.py                  # Path setup + marker registration
├── _paths.py                    # Layout-aware path helpers (host + container)
├── fixtures/
│   ├── db.py                    # `db` AsyncSession fixture (transactional rollback per test)
│   └── factories.py             # make_agent, make_check, make_user, …
├── alerts/                      # Alert subsystem tests
│   ├── test_state_key.py                 # pure — _compute_alert_state_key
│   ├── test_state_key_properties.py      # pure — hypothesis property tests
│   ├── test_dedup_decision.py            # pure — _should_send_notification
│   ├── test_ssl_recovery.py              # pure — _evaluate_ssl_cert_expiry recovery
│   ├── test_dependency_suppression.py    # pure — _handle_parent_suppression
│   └── test_alert_crud_integration.py    # integration — AlertCRUD vs real DB
├── test_architecture.py         # grep-based layering rules (pure)
├── test_no_raw_js_network.py    # JS lint guard (pure)
├── test_no_redundant_db_commit.py  # DB transaction lint guard (pure)
└── test_request_helpers.py      # pure helpers
```

### Why domain organization?

- Adding a new product domain = add a new directory. No structural changes.
- Each domain can contain pure logic, DB integration, and API tests — whatever
  that domain needs.
- Easy to find tests: "where are the alert tests?" → `tests/alerts/`.
- Easy to run by domain: `pytest tests/alerts/`.

---

## Test Tiers

Every test must be tagged with a marker:

| Marker | When to use | Speed | Requires |
|---|---|---|---|
| `@pytest.mark.pure` | Logic that touches no I/O. State-key computation, dedup decision rules, parsing, formatting. | Milliseconds per test. | Nothing. |
| `@pytest.mark.integration` | Anything that hits the DB. CRUD methods, service methods that own transactions, queries that exercise indexes or constraints. | ~tens of ms per test. | `make test-db-up` first. |
| `@pytest.mark.api` | Full request → response. FastAPI TestClient with overridden auth. | ~hundreds of ms per test. | DB up + app importable. |

Use `pytestmark = pytest.mark.<tier>` at the module level (not per-test) when
every test in the file is the same tier — which it almost always is, because
files are organized by tier within a domain.

---

## Anti-Fabrication Strategy

Previous LLM-generated tests in other projects were fabricated: mocked all
dependencies, guessed expected values, used tautological assertions. This
suite uses four defenses:

1. **Hypothesis property tests** verify invariants that can't be faked.
   `test_ssl_band_monotonic_in_days` runs hundreds of random day values and
   asserts that lower days always produce equal-or-tighter bands. The
   property is mathematical — fabricating it requires knowing the answer,
   which defeats the point.

2. **Mutmut mutation testing** changes the source code one line at a time
   (flips `+` to `-`, changes `>` to `>=`, drops a `return`, etc.) and reruns
   the tests. If no test fails, that mutation survived — meaning we have a
   test gap. Target: ≥85% kill rate per module under test. Run with:
   ```
   make mutmut-alerts          # mutate alert_core_service.py
   make mutmut-results         # see surviving mutants
   ```

3. **No mocks in pure logic tests.** If a module is pure logic, test it
   directly. No `unittest.mock`, no patching, no faking. If you need to mock
   something to test it, that module isn't pure and the test belongs in the
   `integration` tier with a real fixture.

4. **Real DB in integration tests.** Integration tests run against an actual
   TimescaleDB (`compose.test.yaml`) on tmpfs. No SQLite. No in-memory fake.
   Indexes, JSONB, hypertables, asyncpg behavior all match production.

### What this catches

Mocking the thing under test catches no bugs. Asserting that the function
returns what you told the mock to return is tautology. Hypothesis + mutmut
+ "no mocks in pure tests" together force tests to be about *behavior* —
which means they break when behavior breaks. The flip side: writing them
takes more thought up front, but each test is then load-bearing.

---

## How to Run

```bash
# Fast feedback loop (pure tests only — no DB needed)
make test-pure                   # tests/ -m pure

# Integration tests (brings the test DB up if needed)
make test-integration            # tests/ -m integration

# Everything
make test-all                    # pure + integration

# Bring the test DB up / down explicitly
make test-db-up                  # docker compose -f compose.test.yaml up -d
make test-db-down                # docker compose -f compose.test.yaml down -v

# Mutation testing (slow — ~minutes per module)
make mutmut-alerts               # mutate services/core/alert_core_service.py
make mutmut-results              # show surviving mutants
```

### Running a single test file or test

```bash
# Inside the test container
docker exec luxswirl_tests python -m pytest tests/alerts/test_state_key.py -v

# A single test
docker exec luxswirl_tests python -m pytest \
  tests/alerts/test_alert_crud_integration.py::TestGetLastNotificationForDedup::test_no_history_returns_none -v

# Filtered by marker
docker exec luxswirl_tests python -m pytest tests/alerts/ -m integration
docker exec luxswirl_tests python -m pytest tests/alerts/ -m "not integration"
```

---

## Writing a New Test

### Pure-logic test (no DB)

```python
"""Tests for foo_module.bar_function."""

from __future__ import annotations

import pytest

from services.core.foo_module import bar_function

pytestmark = pytest.mark.pure


def test_bar_returns_expected_for_simple_input():
    assert bar_function(5) == 10
```

### Integration test (real DB)

```python
"""Integration tests for FooCRUD."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from crud.foo_crud import FooCRUD                       # noqa: E402
from fixtures.factories import make_agent, make_check   # noqa: E402

pytestmark = pytest.mark.integration


async def test_create_foo(db):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    # ... exercise FooCRUD against db ...
```

### Property test (hypothesis)

```python
import pytest
from hypothesis import given, strategies as st

pytestmark = pytest.mark.pure


@given(x=st.integers(min_value=-1000, max_value=1000))
def test_my_invariant_holds_for_all_x(x):
    assert my_function(x) >= 0  # or whatever the invariant is
```

---

## Reference Example

**`tests/alerts/test_alert_crud_integration.py`** is the canonical example
for how to write integration tests in this project. New domain tests should
mirror its shape:

- `pytestmark = pytest.mark.integration` at module level
- Import `db` fixture from `fixtures.db`
- Import factories from `fixtures.factories`
- Test class per CRUD method (`TestGetX`, `TestListY`)
- Each test creates its own scaffolding via factory functions and
  `await db.flush()` — no shared fixtures between tests

If your domain needs new factories, add them to `tests/fixtures/factories.py`
following the existing `make_*` pattern.

---

## Coverage Targets (LUXSWIRL-127)

- `apps/backend/app/services/core/` ≥ 70%
- `apps/backend/app/crud/` ≥ 70%
- `apps/agent/app/checks/` ≥ 70%
- `apps/backend/app/services/views/` ≥ 80% (pure logic — easy to cover)
- `apps/backend/app/web/routers/` ≥ 50% (smoke only — logic lives elsewhere)
- `apps/backend/app/api/v1/routers/` ≥ 50% (same)

Mutmut kill rate target: ≥85% on covered modules.

---

## Related

- LUXSWIRL-127 — tracking issue for bringing coverage to target
- LUXSWIRL-128 — Black + Ruff sweep before OSS publish
- `tests/_paths.py` — layout-aware helpers (host + container)
- `compose.test.yaml` — test DB on tmpfs
