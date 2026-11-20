"""The writers a synthetic judgement guards (an edit naming a parent, a
dependents change, a delete of a parent) finish beside an agent delete or a
bulk delete of the same checks, whichever check's id sorts first, without a
deadlock; and holding checks leaves their agent's row free for its reports and
heartbeats."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.crud.check_crud import CheckCRUD
from app.models.agent_model import Agent
from app.schemas.check_schema import CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
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


async def _check(worker_db, agent_id: UUID, **kw: Any) -> UUID:
    async with worker_db() as s:
        check = make_check(agent_id=agent_id, **kw)
        s.add(check)
        await s.commit()
        return check.id


async def _agent_lockable(worker_db, agent_id: UUID, *, key_share: bool) -> str:
    """Whether another transaction can take the agent's row now, as a report
    (FOR KEY SHARE) or a heartbeat (FOR NO KEY UPDATE) takes it."""
    async with worker_db() as s:
        try:
            stmt = select(Agent.id).where(Agent.id == agent_id)
            stmt = (
                stmt.with_for_update(read=True, key_share=True, nowait=True)
                if key_share
                else stmt.with_for_update(key_share=True, nowait=True)
            )
            await s.execute(stmt)
            await s.rollback()
            return "free"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return f"held:{type(e).__name__}"


@pytest.mark.parametrize("for_delete", [True, False])
async def test_locking_a_check_leaves_its_agent_free(worker_db, for_delete):
    agent_id = await _agent(worker_db)
    check_id = await _check(worker_db, agent_id)
    async with worker_db() as s:
        await CheckCRUD.get_locked_by_ids(s, [check_id], for_delete=for_delete)
        report = await _agent_lockable(worker_db, agent_id, key_share=True)
        heartbeat = await _agent_lockable(worker_db, agent_id, key_share=False)
        await s.rollback()
    assert (report, heartbeat) == ("free", "free")


@pytest.mark.parametrize("for_delete", [True, False])
async def test_locking_many_checks_leaves_their_agents_free(worker_db, for_delete):
    agent_id = await _agent(worker_db)
    check_id = await _check(worker_db, agent_id)
    async with worker_db() as s:
        await CheckCRUD.get_locked_by_ids(s, [check_id], for_delete=for_delete)
        report = await _agent_lockable(worker_db, agent_id, key_share=True)
        heartbeat = await _agent_lockable(worker_db, agent_id, key_share=False)
        await s.rollback()
    assert (report, heartbeat) == ("free", "free")


# An edit that points a check at a sibling parent, while the agent (or the
# pair) is deleted.

FINISH_SECONDS = 20
PAUSE_SECONDS = 1.5


def _failure(e: Exception) -> str:
    return f"error:{type(e).__name__}:{getattr(e, 'orig', e)!r}"[:300]


def _pause_before_saving(monkeypatch) -> None:
    """The edit's work between holding its row and writing it (its dependency
    and script judged, its config merged), stretched so the race is certain."""
    real = CheckCRUD.save_check

    async def slow_save(db, check):
        await asyncio.sleep(PAUSE_SECONDS)
        return await real(db, check)

    monkeypatch.setattr(CheckCRUD, "save_check", staticmethod(slow_save))


async def _pair(worker_db, *, parent_first: bool):
    agent_id = await _agent(worker_db)
    low, high = sorted([uuid4(), uuid4()])
    parent_id, child_id = (low, high) if parent_first else (high, low)
    await _check(worker_db, agent_id, id=parent_id, display_name="parent")
    await _check(worker_db, agent_id, id=child_id, display_name="child")
    return agent_id, parent_id, child_id


async def _edit(worker_db, check_id: UUID, data: CheckUpdate) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(s, check_id, data, actor_is_admin=True)
            await s.commit()
            return "edited"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _delete_agent(worker_db, agent_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=True)
            await s.commit()
            return "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _bulk_delete(worker_db, check_ids: list[UUID]) -> str:
    async with worker_db() as s:
        try:
            out = await CheckCoreService.bulk_delete(s, check_ids, actor_is_admin=True)
            await s.commit()
            return f"bulk:{out['success_count']}/{out['failure_count']}:{out['errors']}"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _race(edit, deleter) -> dict[str, str]:
    editing = asyncio.create_task(edit())
    await asyncio.sleep(0.5)  # the edit holds its row and is pausing
    deleting = asyncio.create_task(deleter())
    return {
        "edit": await asyncio.wait_for(editing, FINISH_SECONDS),
        "delete": await asyncio.wait_for(deleting, FINISH_SECONDS),
    }


async def test_an_edit_naming_a_sibling_parent_and_an_agent_delete_do_not_deadlock(
    worker_db, monkeypatch
):
    agent_id, parent_id, child_id = await _pair(worker_db, parent_first=True)
    _pause_before_saving(monkeypatch)
    outcome = await _race(
        lambda: _edit(worker_db, child_id, CheckUpdate(depends_on_check_id=parent_id)),
        lambda: _delete_agent(worker_db, agent_id),
    )
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome


async def test_an_edit_naming_a_sibling_parent_and_a_bulk_delete_do_not_deadlock(
    worker_db, monkeypatch
):
    _, parent_id, child_id = await _pair(worker_db, parent_first=True)
    _pause_before_saving(monkeypatch)
    outcome = await _race(
        lambda: _edit(worker_db, child_id, CheckUpdate(depends_on_check_id=parent_id)),
        lambda: _bulk_delete(worker_db, [parent_id, child_id]),
    )
    assert "deadlock" not in str(outcome).lower(), outcome


async def test_an_edit_of_another_field_and_an_agent_delete_both_finish(
    worker_db, monkeypatch
):
    agent_id, _, child_id = await _pair(worker_db, parent_first=True)
    _pause_before_saving(monkeypatch)
    outcome = await _race(
        lambda: _edit(worker_db, child_id, CheckUpdate(interval_seconds=600)),
        lambda: _delete_agent(worker_db, agent_id),
    )
    assert outcome == {"edit": "edited", "delete": "deleted"}, outcome


async def test_an_edit_naming_a_parent_that_sorts_after_it_and_an_agent_delete_both_finish(
    worker_db, monkeypatch
):
    agent_id, parent_id, child_id = await _pair(worker_db, parent_first=False)
    _pause_before_saving(monkeypatch)
    outcome = await _race(
        lambda: _edit(worker_db, child_id, CheckUpdate(depends_on_check_id=parent_id)),
        lambda: _delete_agent(worker_db, agent_id),
    )
    assert outcome == {"edit": "edited", "delete": "deleted"}, outcome


# A dependents change while the pair is deleted in id order.


def _pause_after_holding_the_parent(monkeypatch) -> None:
    real = CheckCRUD.list_dependents

    async def slow_list(db, parent_check_id):
        await asyncio.sleep(PAUSE_SECONDS)
        return await real(db, parent_check_id)

    monkeypatch.setattr(CheckCRUD, "list_dependents", staticmethod(slow_list))


async def _set_dependents(worker_db, parent_id: UUID, child_ids: list[UUID]) -> str:
    async with worker_db() as s:
        try:
            await CheckDependencyCoreService.set_dependents(
                s, parent_id, child_ids, actor_is_admin=True
            )
            await s.commit()
            return "set"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def test_a_dependents_change_and_an_agent_delete_do_not_deadlock(
    worker_db, monkeypatch
):
    """The child's id sorts before its parent's."""
    agent_id, parent_id, child_id = await _pair(worker_db, parent_first=False)
    _pause_after_holding_the_parent(monkeypatch)
    outcome = await _race(
        lambda: _set_dependents(worker_db, parent_id, [child_id]),
        lambda: _delete_agent(worker_db, agent_id),
    )
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome


async def test_a_dependents_change_and_a_bulk_delete_do_not_deadlock(
    worker_db, monkeypatch
):
    _, parent_id, child_id = await _pair(worker_db, parent_first=False)
    _pause_after_holding_the_parent(monkeypatch)
    outcome = await _race(
        lambda: _set_dependents(worker_db, parent_id, [child_id]),
        lambda: _bulk_delete(worker_db, [parent_id, child_id]),
    )
    assert "deadlock" not in str(outcome).lower(), outcome


async def test_a_dependents_change_whose_child_sorts_after_its_parent_and_an_agent_delete_do_not_deadlock(
    worker_db, monkeypatch
):
    agent_id, parent_id, child_id = await _pair(worker_db, parent_first=True)
    _pause_after_holding_the_parent(monkeypatch)
    outcome = await _race(
        lambda: _set_dependents(worker_db, parent_id, [child_id]),
        lambda: _delete_agent(worker_db, agent_id),
    )
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome


# A single delete of a parent whose child sorts first, while their agent is
# deleted.


async def _delete_check(worker_db, check_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.delete_check(s, check_id, actor_is_admin=True)
            await s.commit()
            return "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def test_a_parent_delete_and_its_agents_delete_do_not_deadlock(
    worker_db, monkeypatch
):
    agent_id, parent_id, child_id = await _pair(worker_db, parent_first=False)
    async with worker_db() as s:
        await CheckCoreService.update_check(
            s, child_id, CheckUpdate(depends_on_check_id=parent_id), actor_is_admin=True
        )
        await s.commit()
    real = CheckCRUD.load_agent

    async def slow_load(db, check):
        await asyncio.sleep(PAUSE_SECONDS)
        return await real(db, check)

    monkeypatch.setattr(CheckCRUD, "load_agent", staticmethod(slow_load))
    outcome = await _race(
        lambda: _delete_check(worker_db, parent_id),
        lambda: _delete_agent(worker_db, agent_id),
    )
    assert "deadlock" not in str(outcome).lower(), outcome
