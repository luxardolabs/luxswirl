"""Unit tests for DISTRIBUTE-mode agent assignment (LUXSWIRL-183).

Pure constructor tests — ``make_agent``/``make_check`` build transient ORM
instances, no DB session needed. The invariant under test: the assignment is a
deterministic, process-independent function of the immutable UUIDs
(``check.id``, ``agent.id``), not of ``display_name`` or the Python hash seed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent, make_check  # noqa: E402

from app.services.core.agent_assignment_core_service import (  # noqa: E402
    AgentAssignmentCoreService,
)

assign = AgentAssignmentCoreService.get_assigned_agent_for_check


def _pool(n: int) -> list:
    return [make_agent(agent_name=f"agent-{i:02d}") for i in range(n)]


def test_empty_pool_returns_none() -> None:
    check = make_check(agent_id=uuid4())
    assert assign(check, []) is None


def test_single_agent_always_wins() -> None:
    agents = _pool(1)
    check = make_check(agent_id=uuid4())
    assert assign(check, agents).id == agents[0].id


def test_deterministic_within_process() -> None:
    agents = _pool(5)
    check = make_check(agent_id=uuid4())
    first = assign(check, agents).id
    assert all(assign(check, agents).id == first for _ in range(20))


def test_order_invariant() -> None:
    """Shuffling the candidate list must not change the winner (HRW is a set op)."""
    agents = _pool(7)
    check = make_check(agent_id=uuid4())
    expected = assign(check, agents).id
    for shift in range(1, len(agents)):
        rotated = agents[shift:] + agents[:shift]
        assert assign(check, rotated).id == expected
    assert assign(check, list(reversed(agents))).id == expected


def test_rename_invariant() -> None:
    """Same check.id, different display_name -> same agent (keyed on id, not name)."""
    agents = _pool(5)
    cid = uuid4()
    a = assign(make_check(agent_id=uuid4(), id=cid, display_name="alpha"), agents).id
    b = assign(make_check(agent_id=uuid4(), id=cid, display_name="totally-renamed"), agents).id
    assert a == b


def test_distribution_is_roughly_even() -> None:
    agents = _pool(4)
    counts: dict[UUID, int] = {a.id: 0 for a in agents}
    for _ in range(4000):
        counts[assign(make_check(agent_id=uuid4()), agents).id] += 1
    # Expect ~1000 each; generous slack for hash variance.
    for c in counts.values():
        assert 750 < c < 1250, counts


def test_hrw_minimal_reshuffle_on_pool_change() -> None:
    """HRW property: removing the NON-owner leaves the check put; removing the
    OWNER moves it to exactly one survivor — checks on other agents don't move."""
    agents = _pool(5)
    check = make_check(agent_id=uuid4())
    owner = assign(check, agents)

    # Drop a non-owner -> winner unchanged.
    non_owner = next(a for a in agents if a.id != owner.id)
    remaining = [a for a in agents if a.id != non_owner.id]
    assert assign(check, remaining).id == owner.id

    # Drop the owner -> reassigned to some survivor, deterministically.
    survivors = [a for a in agents if a.id != owner.id]
    new_owner = assign(check, survivors)
    assert new_owner.id != owner.id
    assert assign(check, survivors).id == new_owner.id


# Fixed UUIDs so the subprocess result is comparable across seeds.
_CHECK_ID = "11111111-1111-1111-1111-111111111111"
_AGENT_IDS = [
    "aaaaaaaa-0000-0000-0000-000000000001",
    "aaaaaaaa-0000-0000-0000-000000000002",
    "aaaaaaaa-0000-0000-0000-000000000003",
    "aaaaaaaa-0000-0000-0000-000000000004",
]
_PROBE = Path(__file__).resolve().parent.parent / "fixtures" / "_assign_probe.py"
_COMPONENT_ROOT = Path(__file__).resolve().parents[2]  # apps/backend (has app/)


def _assign_in_subprocess(hashseed: str) -> str:
    env = {
        **os.environ,
        "PYTHONHASHSEED": hashseed,
        # Fresh interpreter: put the backend root (for app.*) and tests root
        # (for fixtures.*) on the path explicitly.
        "PYTHONPATH": os.pathsep.join([str(_COMPONENT_ROOT), _tests_root]),
    }
    proc = subprocess.run(
        [sys.executable, str(_PROBE), _CHECK_ID, *_AGENT_IDS],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    # The probe imports app, which initializes structured logging to stdout, so
    # the chosen id is the last printed line (it's print()ed after all imports).
    return proc.stdout.strip().splitlines()[-1].strip()


def test_deterministic_across_hash_seeds() -> None:
    """The regression guard against reverting to builtin hash().

    Runs the assignment in fresh interpreters under different PYTHONHASHSEED
    values and asserts an identical winner. Builtin hash() over a str is
    seed-salted, so a revert would make these diverge; sha256 does not.
    """
    results = {_assign_in_subprocess(seed) for seed in ("0", "1", "42")}
    assert len(results) == 1, f"assignment varied by hash seed: {results}"
    # ...and it matches the in-process result for the same fixed inputs.
    agents = [make_agent(id=UUID(a), agent_name=a) for a in _AGENT_IDS]
    check = make_check(agent_id=UUID(_CHECK_ID), id=UUID(_CHECK_ID))
    assert results.pop() == str(assign(check, agents).id)
