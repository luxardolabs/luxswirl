"""The synthetic judgement's guard neither deadlocks with the writers that
share its rows nor holds them through slow work.

A check created for an agent being deleted does not deadlock with the
delete; an edit by an admin of a check that arrives finishes beside the
agent's delete; a dependents change and a delete of its parent do not
deadlock; an edit looks up its endpoints, and refuses an editor, before it
takes the guard; and a check listed twice in a bulk move is moved once and
stays moved.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any
from uuid import UUID

import pytest
from app.core.exceptions import AuthorizationException, CheckBusyException
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.models.check_model import Check
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy import select

pytestmark = pytest.mark.integration

FINISH_SECONDS = 15
TARGET = "https://127.0.0.1/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)
# Longer than the web pool's statement_timeout (5 s): a lock held this long
# fails the request waiting behind it.
SLOW_LOOKUP_SECONDS = 9
# A name only this file's resolver answers.
SLOW_HOST = "slow.example.test"


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


async def _agent_of(worker_db, check_id: UUID) -> UUID | None:
    async with worker_db() as s:
        return (
            await s.execute(select(Check.agent_id).where(Check.id == check_id))
        ).scalar_one_or_none()


def _failure(e: Exception) -> str:
    return f"error:{type(e).__name__}:{getattr(e, 'orig', e)!r}"[:300]


async def _delete_agent(worker_db, agent_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=True)
            await s.commit()
            return "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def test_a_check_created_during_its_agents_delete_does_not_deadlock(
    worker_db,
):
    """A heartbeat holds the agent while its delete starts; a plain check is
    created for the agent meanwhile. The delete finishes, and the create
    either lands (and goes with the agent) or is refused; neither deadlocks."""
    agent_id = await _agent(worker_db)
    await _check(worker_db, agent_id, display_name="old")

    async def create() -> str:
        async with worker_db() as s:
            try:
                await CheckCoreService.create_check(
                    s,
                    agent_id,
                    CheckCreate(display_name="new", check_type="http", target=TARGET),
                    actor_is_admin=True,
                )
                await s.commit()
                return "created"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return _failure(e)

    async with worker_db() as heartbeat:
        await AgentCRUD.lock_without_key(heartbeat, agent_id)
        delete = asyncio.create_task(_delete_agent(worker_db, agent_id))
        await asyncio.sleep(0.7)  # the delete holds the agent's guard
        creating = asyncio.create_task(create())
        await asyncio.sleep(0.5)
        await heartbeat.commit()

    outcome = {
        "delete": await asyncio.wait_for(delete, FINISH_SECONDS),
        "create": await asyncio.wait_for(creating, FINISH_SECONDS),
    }
    assert outcome["delete"] == "deleted", outcome
    assert "deadlock" not in str(outcome).lower(), outcome


async def test_an_agent_delete_and_an_edit_of_a_check_that_just_arrived_do_not_deadlock(
    worker_db,
):
    """An admin's edit locks the arriving check, then (stamping the agent's
    checks as changed) the agent's row."""
    agent_id = await _agent(worker_db)
    await _check(worker_db, agent_id, display_name="old")

    async def edit(check_id: UUID) -> str:
        async with worker_db() as s:
            try:
                await CheckCoreService.update_check(
                    s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=True
                )
                await s.commit()
                return "edited"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return _failure(e)

    async with worker_db() as heartbeat:
        await AgentCRUD.lock_without_key(heartbeat, agent_id)
        delete = asyncio.create_task(_delete_agent(worker_db, agent_id))
        await asyncio.sleep(0.7)
        new_id = await asyncio.wait_for(
            _check(worker_db, agent_id, display_name="new"), 5
        )
        editing = asyncio.create_task(edit(new_id))
        await asyncio.sleep(0.7)  # the edit holds "new" and waits for the agent
        await heartbeat.commit()
    outcome = {
        "edit": await asyncio.wait_for(editing, FINISH_SECONDS),
        "delete": await asyncio.wait_for(delete, FINISH_SECONDS),
    }
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome


async def test_a_check_listed_twice_in_a_bulk_move_is_moved(worker_db):
    source = await _agent(worker_db)
    target = await _agent(worker_db)
    check_id = await _check(worker_db, source, display_name="mover")
    async with worker_db() as s:
        outcome = await CheckCoreService.bulk_modify(
            s,
            [check_id, check_id],
            CheckUpdate(interval_seconds=600),
            new_agent_id=target,
            actor_is_admin=True,
        )
        await s.commit()

    assert outcome["failure_count"] == 0, outcome
    assert await _agent_of(worker_db, check_id) == target, outcome


def _slow_lookups(monkeypatch, seconds: float) -> None:
    """SLOW_HOST resolves to 127.0.0.1 after `seconds`, as a resolver that is
    slow to answer; every other name resolves as it does."""
    real = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != SLOW_HOST:
            return real(host, *args, **kwargs)
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def test_an_alert_change_is_judged_while_an_edit_resolves_its_new_target(
    worker_db, monkeypatch
):
    _slow_lookups(monkeypatch, 2.0)
    agent_id = await _agent(worker_db)
    check_id = await _check(worker_db, agent_id, check_type="http", target=TARGET)

    async def edit() -> None:
        async with worker_db() as s:
            await CheckCoreService.update_check(
                s,
                check_id,
                CheckUpdate(target=f"https://{SLOW_HOST}/"),
                actor_is_admin=True,
            )
            await s.commit()

    editing = asyncio.create_task(edit())
    await asyncio.sleep(0.5)
    async with worker_db() as s:
        try:
            await AlertCoreService.require_checks(s, [check_id], actor_is_admin=False)
            verdict = "judged"
        except CheckBusyException:
            verdict = "busy"
        await s.rollback()
    await asyncio.wait_for(editing, FINISH_SECONDS)

    assert verdict == "judged"


async def test_an_editors_refused_synthetic_edit_does_not_hold_the_check(
    worker_db, monkeypatch
):
    """The editor's change would resolve a new target slowly: it is refused
    first, so an admin's delete of the check is not kept waiting."""
    _slow_lookups(monkeypatch, SLOW_LOOKUP_SECONDS)
    agent_id = await _agent(worker_db)
    synthetic_id = await _check(
        worker_db,
        agent_id,
        display_name="browse",
        check_type="synthetic",
        target=TARGET,
        script_code=SCRIPT,
    )

    async def editor() -> str:
        async with worker_db() as s:
            try:
                await CheckCoreService.update_check(
                    s,
                    synthetic_id,
                    CheckUpdate(target=f"https://{SLOW_HOST}/"),
                    actor_is_admin=False,
                )
            except AuthorizationException:
                await s.rollback()
                return "refused"
            await s.commit()
            return "edited"

    task = asyncio.create_task(editor())
    await asyncio.sleep(0.5)
    async with worker_db() as s:
        try:
            await CheckCoreService.delete_check(s, synthetic_id, actor_is_admin=True)
            await s.commit()
            admin = "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            admin = _failure(e)

    assert await asyncio.wait_for(task, SLOW_LOOKUP_SECONDS + 30) == "refused"
    assert admin == "deleted"


async def test_a_parent_delete_during_a_dependents_change_does_not_deadlock(worker_db):
    """The change detaches the parent's current child and attaches another,
    while the parent's delete (which clears its children) holds the parent."""
    agent_id = await _agent(worker_db)
    parent = await _check(worker_db, agent_id, display_name="parent")
    await _check(worker_db, agent_id, display_name="old", depends_on_check_id=parent)
    new_child = await _check(worker_db, agent_id, display_name="new")

    async def set_children() -> str:
        async with worker_db() as s:
            try:
                await CheckDependencyCoreService.set_dependents(
                    s, parent, [new_child], actor_is_admin=True
                )
                await s.commit()
                return "set"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return _failure(e)

    async with worker_db() as deleter:
        # What delete_check takes first: the agent's synthetic guard, then the
        # parent's row FOR UPDATE.
        await CheckCRUD.hold_synthetic_guard(deleter, {agent_id})
        await CheckCRUD.lock_for_delete(deleter, check_ids=[parent])
        task = asyncio.create_task(set_children())
        await asyncio.sleep(1.5)
        try:
            await CheckCoreService.delete_check(deleter, parent, actor_is_admin=True)
            await deleter.commit()
            deleted = "deleted"
        except Exception as e:  # the outcome is what is asserted
            await deleter.rollback()
            deleted = _failure(e)
    setter = await asyncio.wait_for(task, 20)

    assert "deadlock" not in (deleted + setter).lower(), (deleted, setter)
    assert deleted == "deleted", (deleted, setter)
