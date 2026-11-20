"""A synthetic check's writers wait for what they lock without failing: a bulk
enable or disable behind a held check reports failures, never an error; an edit
never holds its check while it looks up an address, even when the check changed
under it; and an edit that attaches a parent finishes beside its agent's delete.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any
from uuid import UUID

import pytest
from app.crud.check_crud import CheckCRUD
from app.schemas.check_schema import CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import text

pytestmark = pytest.mark.integration

TARGET = "https://127.0.0.1/"
SLOW_HOST = "slow.writes.example.test"
FAST_HOST = "fast.writes.example.test"
# Past the web pool's 5 s statement_timeout.
SLOW_SECONDS = 8.0


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


def _failure(e: Exception) -> str:
    return f"error:{type(e).__name__}:{getattr(e, 'orig', e)!r}"[:300]


def _resolver(monkeypatch) -> None:
    real = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host == SLOW_HOST:
            time.sleep(SLOW_SECONDS)
        elif host != FAST_HOST:
            return real(host, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def _hold_for_delete(worker_db, check_id: UUID, seconds: float) -> None:
    """What a bulk delete job does first (no statement_timeout): its checks
    FOR UPDATE, held through the long cascade."""
    async with worker_db() as s:
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        await CheckCRUD.lock_for_delete(s, check_ids=[check_id])
        await asyncio.sleep(seconds)
        await s.rollback()


async def _bulk_disable(worker_db, check_id: UUID, *, admin: bool) -> Any:
    async with worker_db() as s:
        try:
            outcome = await CheckCoreService.bulk_set_enabled(
                s, [check_id], enabled=False, actor_is_admin=admin
            )
            await s.commit()
            return outcome
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


@pytest.mark.parametrize("admin", [True, False], ids=["admin", "editor"])
async def test_a_bulk_disable_behind_a_held_check_reports_failures_not_a_500(
    worker_db, admin
):
    """A bulk delete job holds the check past the web statement_timeout; the
    bulk disable's wait, its judgement's included, comes back as failures."""
    agent_id = await _agent(worker_db)
    check_id = await _check(worker_db, agent_id, check_type="http", target=TARGET)
    holder = asyncio.create_task(_hold_for_delete(worker_db, check_id, 7.0))
    await asyncio.sleep(0.5)
    outcome = await _bulk_disable(worker_db, check_id, admin=admin)
    await asyncio.wait_for(holder, 20)

    assert isinstance(outcome, dict), outcome
    assert outcome["failure_count"] == 1, outcome


async def _edit_target(worker_db, check_id: UUID, host: str) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(
                s,
                check_id,
                CheckUpdate(target=f"https://{host}/"),
                actor_is_admin=False,
            )
            await s.commit()
            return "edited"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _admin_delete(worker_db, check_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.delete_check(s, check_id, actor_is_admin=True)
            await s.commit()
            return "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


@pytest.mark.parametrize("racing_edit", [False, True], ids=["control", "racing-edit"])
async def test_an_edit_whose_check_changed_meanwhile_does_not_hold_it_through_lookups(
    worker_db, monkeypatch, racing_edit
):
    """An editor's edit to a slow-resolving target; a second edit of the same
    check (another tab, another editor) lands while the first resolves, so the
    first vets again. An admin's delete of the check is not kept waiting past
    the web statement_timeout by those lookups."""
    _resolver(monkeypatch)
    agent_id = await _agent(worker_db)
    check_id = await _check(worker_db, agent_id, check_type="http", target=TARGET)

    slow = asyncio.create_task(_edit_target(worker_db, check_id, SLOW_HOST))
    await asyncio.sleep(0.5)
    if racing_edit:
        assert await _edit_target(worker_db, check_id, FAST_HOST) == "edited"
    # The slow edit's first lookup ends at ~SLOW_SECONDS; it then holds the row.
    await asyncio.sleep(SLOW_SECONDS + 0.5)
    deleted = await _admin_delete(worker_db, check_id)
    edited = await asyncio.wait_for(slow, SLOW_SECONDS * 3)

    assert deleted == "deleted", (deleted, edited)


async def _delete_agent_job(worker_db, agent_id: UUID) -> str:
    """As the agent_delete maintenance job runs it: no statement_timeout."""
    async with worker_db() as s:
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        try:
            await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=True)
            await s.commit()
            return "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


@pytest.mark.parametrize("racing_edit", [False, True], ids=["control", "racing-edit"])
async def test_an_edit_that_attaches_a_parent_and_an_agent_delete_do_not_deadlock(
    worker_db, monkeypatch, racing_edit
):
    """An edit of check C sets a slow-resolving target and attaches parent P
    (same agent, lower id); the agent's delete job (which locks P, then C) runs
    meanwhile."""
    _resolver(monkeypatch)
    agent_id = await _agent(worker_db)
    a = await _check(worker_db, agent_id, check_type="http", target=TARGET)
    b = await _check(worker_db, agent_id, check_type="http", target=TARGET)
    parent, child = sorted([a, b])

    async def edit() -> str:
        async with worker_db() as s:
            try:
                await CheckCoreService.update_check(
                    s,
                    child,
                    CheckUpdate(
                        target=f"https://{SLOW_HOST}/", depends_on_check_id=parent
                    ),
                    actor_is_admin=False,
                )
                await s.commit()
                return "edited"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return _failure(e)

    editing = asyncio.create_task(edit())
    await asyncio.sleep(0.5)
    if racing_edit:
        assert await _edit_target(worker_db, child, FAST_HOST) == "edited"
    await asyncio.sleep(SLOW_SECONDS + 0.5)
    deleting = asyncio.create_task(_delete_agent_job(worker_db, agent_id))
    outcome = {
        "edit": await asyncio.wait_for(editing, SLOW_SECONDS * 3),
        "delete": await asyncio.wait_for(deleting, SLOW_SECONDS * 3),
    }

    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome
