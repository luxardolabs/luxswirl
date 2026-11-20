"""Dependencies stay single-level under concurrent writes on different agents.

A write making a check depend on a parent takes the dependency lock on both,
so an edit making the parent a child, or the child a parent, at the same time
is judged after it on the committed links and refused: no two writes commit a
two-level chain, whichever agents (and synthetic guards) they are on.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from app.models.check_model import Check
from app.schemas.check_schema import CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _check(worker_db, agent_id: UUID, name: str) -> UUID:
    async with worker_db() as s:
        check = make_check(agent_id=agent_id, display_name=name)
        s.add(check)
        await s.commit()
        return check.id


async def _parents(worker_db, ids: list[UUID]) -> dict[UUID, UUID | None]:
    async with worker_db() as s:
        rows = await s.execute(
            select(Check.id, Check.depends_on_check_id).where(Check.id.in_(ids))
        )
        return {r[0]: r[1] for r in rows}


async def _second(worker_db, write) -> str:
    """Run the second write in its own session; its outcome as text."""
    async with worker_db() as s:
        try:
            await write(s)
            await s.commit()
            return "committed"
        except Exception as exc:  # the outcome is what the test reports
            await s.rollback()
            return type(exc).__name__


async def _race(worker_db, first, second) -> tuple[str, str]:
    """`first` writes and stays open; `second` runs beside it; then `first`
    commits. Returns (second's state when first committed, second's outcome)."""
    async with worker_db() as s1:
        await first(s1)
        task = asyncio.create_task(_second(worker_db, second))
        done, _ = await asyncio.wait({task}, timeout=2)
        state = "finished" if done else "waiting"
        await s1.commit()
    return state, await asyncio.wait_for(task, 30)


def _is_chain(parents: dict[UUID, UUID | None]) -> bool:
    """A check that is someone's parent has a parent of its own."""
    used = {p for p in parents.values() if p is not None}
    return any(parents.get(p) is not None for p in used)


async def test_parent_becomes_child_while_attached_on_other_agent(worker_db):
    """T1: child C -> parent P (P only KEY SHARE). T2: P -> Q on P's agent."""
    a1, a2 = await _agent(worker_db), await _agent(worker_db)
    c = await _check(worker_db, a1, "c")
    p = await _check(worker_db, a2, "p")
    q = await _check(worker_db, a2, "q")

    async def first(s):
        await CheckCoreService.update_check(
            s, c, CheckUpdate(depends_on_check_id=p), actor_is_admin=False
        )

    async def second(s):
        await CheckCoreService.update_check(
            s, p, CheckUpdate(depends_on_check_id=q), actor_is_admin=False
        )

    state, outcome = await _race(worker_db, first, second)
    parents = await _parents(worker_db, [c, p, q])
    assert not _is_chain(parents), (state, outcome, parents)


async def test_child_becomes_parent_while_attached_on_other_agent(worker_db):
    """T1: C -> P (C held FOR NO KEY UPDATE). T2: X -> C (C only KEY SHARE)."""
    a1, a2 = await _agent(worker_db), await _agent(worker_db)
    c = await _check(worker_db, a1, "c")
    p = await _check(worker_db, a1, "p")
    x = await _check(worker_db, a2, "x")

    async def first(s):
        await CheckCoreService.update_check(
            s, c, CheckUpdate(depends_on_check_id=p), actor_is_admin=False
        )

    async def second(s):
        await CheckCoreService.update_check(
            s, x, CheckUpdate(depends_on_check_id=c), actor_is_admin=False
        )

    state, outcome = await _race(worker_db, first, second)
    parents = await _parents(worker_db, [c, p, x])
    assert not _is_chain(parents), (state, outcome, parents)


async def test_set_dependents_beside_parent_made_a_child(worker_db):
    """T1: PUT P's dependents = [C] (P KEY SHARE). T2: P -> Q on P's agent."""
    a1, a2 = await _agent(worker_db), await _agent(worker_db)
    c = await _check(worker_db, a1, "c")
    p = await _check(worker_db, a2, "p")
    q = await _check(worker_db, a2, "q")

    async def first(s):
        await CheckDependencyCoreService.set_dependents(s, p, [c], actor_is_admin=False)

    async def second(s):
        await CheckCoreService.update_check(
            s, p, CheckUpdate(depends_on_check_id=q), actor_is_admin=False
        )

    state, outcome = await _race(worker_db, first, second)
    parents = await _parents(worker_db, [c, p, q])
    assert not _is_chain(parents), (state, outcome, parents)


async def test_create_with_parent_beside_parent_made_a_child(worker_db):
    """T1: C -> P (edit). T2: create X with parent C on another agent: the
    create holds no parent row, so it judges C as committed (no parent)."""
    from app.schemas.check_schema import CheckCreate

    a1, a2 = await _agent(worker_db), await _agent(worker_db)
    c = await _check(worker_db, a1, "c")
    p = await _check(worker_db, a1, "p")
    made: list[UUID] = []

    async def first(s):
        await CheckCoreService.update_check(
            s, c, CheckUpdate(depends_on_check_id=p), actor_is_admin=False
        )

    async def second(s):
        x = await CheckCoreService.create_check(
            s,
            a2,
            CheckCreate(
                display_name="x",
                check_type="ping",
                target="example.com",
                depends_on_check_id=c,
            ),
            actor_is_admin=False,
        )
        made.append(x.id)

    state, outcome = await _race(worker_db, first, second)
    parents = await _parents(worker_db, [c, p, *made])
    assert not _is_chain(parents), (state, outcome, parents)


async def test_same_agent_keeps_single_level(worker_db):
    """Control: all three on one agent, the synthetic guard serializes."""
    a1 = await _agent(worker_db)
    c = await _check(worker_db, a1, "c")
    p = await _check(worker_db, a1, "p")
    q = await _check(worker_db, a1, "q")

    async def first(s):
        await CheckCoreService.update_check(
            s, c, CheckUpdate(depends_on_check_id=p), actor_is_admin=False
        )

    async def second(s):
        await CheckCoreService.update_check(
            s, p, CheckUpdate(depends_on_check_id=q), actor_is_admin=False
        )

    state, outcome = await _race(worker_db, first, second)
    parents = await _parents(worker_db, [c, p, q])
    assert not _is_chain(parents), (state, outcome, parents)
    assert (state, outcome) == ("waiting", "ValidationException"), (state, outcome)
