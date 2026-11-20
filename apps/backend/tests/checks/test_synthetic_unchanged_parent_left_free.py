"""A write on a child that leaves its parent as it is does not hold the
parent, so a delete of the parent beside it finishes.

A child on agent C depends on a parent on agent A. An editor's write that
names the parent the child already has (the web edit form resubmits it; a
dependents PUT replaces the whole set, current children included) or detaches
the child reads nothing of the parent. A delete of the parent (single, bulk or
its agent's) holds the parent FOR UPDATE and then needs the child's row for
the foreign key's ON DELETE SET NULL; the two take different synthetic guards,
so the writer holding the parent too would deadlock with it whichever id sorts
first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.schemas.check_schema import CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PAUSE = 1.5
FINISH = 25

Change = Callable[[AsyncSession], Awaitable[Any]]


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _setup(worker_db, *, child_first: bool) -> tuple[UUID, UUID, UUID]:
    """Parent on agent A, child on agent C depending on it. Returns
    (agent A, parent, child)."""
    agent_a = await _agent(worker_db)
    agent_c = await _agent(worker_db)
    low, high = sorted([uuid4(), uuid4()])
    child_id, parent_id = (low, high) if child_first else (high, low)
    async with worker_db() as s:
        s.add(make_check(agent_id=agent_a, id=parent_id, display_name="parent"))
        await s.commit()
    async with worker_db() as s:
        s.add(
            make_check(
                agent_id=agent_c,
                id=child_id,
                display_name="child",
                depends_on_check_id=parent_id,
            )
        )
        await s.commit()
    return agent_a, parent_id, child_id


def _task(worker_db, change: Change) -> asyncio.Task[str]:
    async def run() -> str:
        async with worker_db() as s:
            try:
                out = await change(s)
                await s.commit()
                return f"done:{out}" if out is not None else "done"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return f"error:{type(e).__name__}:{getattr(e, 'orig', e)!r}"[:300]

    return asyncio.create_task(run())


def _pause_first(
    monkeypatch, owner: Any, name: str
) -> tuple[asyncio.Event, asyncio.Event]:
    original = getattr(owner, name)
    paused, resume = asyncio.Event(), asyncio.Event()
    first = [True]

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if first[0]:
            first[0] = False
            paused.set()
            await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, name, staticmethod(wrapper))
    return paused, resume


# The deleters, each paused after it holds the parent's row.


def _single_delete(monkeypatch, agent_a, parent_id, child_id):
    paused, resume = _pause_first(monkeypatch, CheckCRUD, "load_agent")

    async def run(s: AsyncSession) -> None:
        await CheckCoreService.delete_check(s, parent_id, actor_is_admin=False)

    return paused, resume, run


def _bulk_delete(monkeypatch, agent_a, parent_id, child_id):
    paused, resume = _pause_first(monkeypatch, CheckCRUD, "bulk_delete_by_ids")

    async def run(s: AsyncSession) -> str:
        out = await CheckCoreService.bulk_delete(s, [parent_id], actor_is_admin=False)
        return f"{out['success_count']}/{out['failure_count']}:{out['errors']}"

    return paused, resume, run


def _agent_delete(monkeypatch, agent_a, parent_id, child_id):
    paused, resume = _pause_first(monkeypatch, AgentCRUD, "delete_agent")

    async def run(s: AsyncSession) -> None:
        await AgentCoreService.delete_agent(s, agent_a, actor_is_admin=False)

    return paused, resume, run


# The editor's writes on the child.


def _edit_naming_parent(parent_id, child_id) -> Change:
    """The web edit form: the child's current parent resubmitted."""

    async def run(s: AsyncSession) -> None:
        await CheckCoreService.update_check(
            s,
            child_id,
            CheckUpdate(interval_seconds=600, depends_on_check_id=parent_id),
            actor_is_admin=False,
        )

    return run


def _edit_other_field(parent_id, child_id) -> Change:
    async def run(s: AsyncSession) -> None:
        await CheckCoreService.update_check(
            s, child_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
        )

    return run


def _dependents_clear(parent_id, child_id) -> Change:
    """PUT dependents: an empty set, its current child detached."""

    async def run(s: AsyncSession) -> None:
        await CheckDependencyCoreService.set_dependents(
            s, parent_id, [], actor_is_admin=False
        )

    return run


def _dependents_put(parent_id, child_id) -> Change:
    """PUT dependents: the whole set, its current child kept."""

    async def run(s: AsyncSession) -> None:
        await CheckDependencyCoreService.set_dependents(
            s, parent_id, [child_id], actor_is_admin=False
        )

    return run


async def _race(worker_db, monkeypatch, deleter_of, writer_of, *, child_first: bool):
    agent_a, parent_id, child_id = await _setup(worker_db, child_first=child_first)
    paused, resume, deleter = deleter_of(monkeypatch, agent_a, parent_id, child_id)
    deleting = _task(worker_db, deleter)
    await asyncio.wait_for(paused.wait(), FINISH)
    # The deleter holds the parent FOR UPDATE; the writer starts now.
    writing = _task(worker_db, writer_of(parent_id, child_id))
    await asyncio.sleep(PAUSE)
    resume.set()
    return {
        "delete": await asyncio.wait_for(deleting, FINISH),
        "write": await asyncio.wait_for(writing, FINISH),
    }


DELETERS = [_single_delete, _bulk_delete, _agent_delete]


WRITERS = [_edit_naming_parent, _dependents_put, _dependents_clear, _edit_other_field]


@pytest.mark.parametrize("child_first", [True, False])
@pytest.mark.parametrize("deleter_of", DELETERS)
@pytest.mark.parametrize("writer_of", WRITERS)
async def test_a_write_leaving_the_parent_finishes_beside_its_delete(
    worker_db, monkeypatch, deleter_of, writer_of, child_first
):
    outcome = await _race(
        worker_db, monkeypatch, deleter_of, writer_of, child_first=child_first
    )
    assert "deadlock" not in str(outcome).lower(), outcome
    assert not outcome["delete"].startswith("error"), outcome
