"""A write that judges a check under its agent's synthetic guard holds the
check's row from its judgement to its end, so the check cannot leave that agent
in between.

A plain bulk move takes no guard on the agent it leaves. Started while an
editor's edit, delete or dependents change sits between its judgement and its
write, the move waits for that write; the editor changed a plain check. An
admin's retype to synthetic of a check an editor's move is carrying to another
agent waits for the move and is answered busy, so it never makes the check
synthetic on an agent whose guard it does not hold, and no editor's write
judged there lands on a synthetic check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from app.core.exceptions import AuthorizationException, CheckBusyException
from app.crud.check_crud import CheckCRUD
from app.models.check_model import Check
from app.schemas.check_schema import CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PAUSE = 1.0
FINISH = 15
TARGET = "https://127.0.0.1/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)

Change = Callable[[AsyncSession], Awaitable[None]]


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _http_check(worker_db, agent_id: UUID, name: str = "browse") -> UUID:
    async with worker_db() as s:
        check = make_check(
            agent_id=agent_id, display_name=name, check_type="http", target=TARGET
        )
        s.add(check)
        await s.commit()
        return check.id


async def _stored(worker_db, check_id: UUID) -> tuple[Any, ...] | None:
    async with worker_db() as s:
        row = (
            await s.execute(
                select(
                    Check.check_type,
                    Check.agent_id,
                    Check.interval_seconds,
                    Check.depends_on_check_id,
                ).where(Check.id == check_id)
            )
        ).one_or_none()
        return tuple(row) if row else None


def _task(worker_db, change: Change) -> asyncio.Task[str]:
    async def run() -> str:
        async with worker_db() as s:
            try:
                await change(s)
                await s.commit()
                return "done"
            except (AuthorizationException, CheckBusyException) as e:
                await s.rollback()
                return f"refused:{type(e).__name__}"
            except Exception as e:  # the outcome is what is asserted
                await s.rollback()
                return f"error:{type(e).__name__}:{e}"[:300]

    return asyncio.create_task(run())


def _move(check_id: UUID, to_agent: UUID) -> Change:
    """An editor's bulk move of a plain check: it takes no synthetic guard."""

    async def run(s: AsyncSession) -> None:
        out = await CheckCoreService.bulk_modify(
            s, [check_id], CheckUpdate(), new_agent_id=to_agent, actor_is_admin=False
        )
        assert out["success_count"] == 1, out

    return run


def _retype(check_id: UUID) -> Change:
    async def run(s: AsyncSession) -> None:
        await CheckCoreService.update_check(
            s,
            check_id,
            CheckUpdate(check_type="synthetic", script_code=SCRIPT, target=TARGET),
            actor_is_admin=True,
        )

    return run


def _edit(check_id: UUID, _other: UUID) -> Change:
    async def run(s: AsyncSession) -> None:
        await CheckCoreService.update_check(
            s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
        )

    return run


def _delete(check_id: UUID, _other: UUID) -> Change:
    async def run(s: AsyncSession) -> None:
        await CheckCoreService.delete_check(s, check_id, actor_is_admin=False)

    return run


def _attach(check_id: UUID, parent_id: UUID) -> Change:
    async def run(s: AsyncSession) -> None:
        await CheckDependencyCoreService.set_dependents(
            s, parent_id, [check_id], actor_is_admin=False
        )

    return run


def _agent_delete(_check_id: UUID, agent_id: UUID) -> Change:
    async def run(s: AsyncSession) -> None:
        await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=False)

    return run


def _pause_first(monkeypatch, name: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Pause the first call of `CheckCRUD.<name>` (the editor's, after its
    judgement and before its write reaches the row) until resumed."""
    original = getattr(CheckCRUD, name)
    paused, resume = asyncio.Event(), asyncio.Event()
    first = [True]

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if first[0]:
            first[0] = False
            paused.set()
            await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(CheckCRUD, name, wrapper)
    return paused, resume


async def _move_during_judged_write(
    worker_db, monkeypatch, pause_at: str, change_of
) -> tuple[bool, str, str, Any, UUID]:
    """An editor's write judged plain on A, paused before its write; a move of
    the check to B started then. Returns whether the move waited for the
    editor, both outcomes, the check as stored and the parent's id."""
    agent_a = await _agent(worker_db)
    agent_b = await _agent(worker_db)
    parent_id = await _http_check(worker_db, agent_a, "parent")
    check_id = await _http_check(worker_db, agent_a)
    paused, resume = _pause_first(monkeypatch, pause_at)
    editor = _task(worker_db, change_of(check_id, parent_id))
    await asyncio.wait_for(paused.wait(), FINISH)
    mover = _task(worker_db, _move(check_id, agent_b))
    await asyncio.sleep(PAUSE)
    move_waited = not mover.done()
    resume.set()
    editor_outcome = await asyncio.wait_for(editor, FINISH)
    move_outcome = await asyncio.wait_for(mover, FINISH)
    return (
        move_waited,
        editor_outcome,
        move_outcome,
        await _stored(worker_db, check_id),
        parent_id,
    )


async def test_a_move_waits_for_an_editors_judged_edit(worker_db, monkeypatch):
    waited, editor, mover, stored, _ = await _move_during_judged_write(
        worker_db, monkeypatch, "save_check", _edit
    )
    assert waited, (editor, mover, stored)
    assert (editor, mover) == ("done", "done")
    assert stored is not None and (stored[0], stored[2]) == ("http", 600)


async def test_a_move_waits_for_an_editors_judged_delete(worker_db, monkeypatch):
    waited, editor, mover, stored, _ = await _move_during_judged_write(
        worker_db, monkeypatch, "load_agent", _delete
    )
    assert waited, (editor, mover, stored)
    assert editor == "done" and stored is None, (editor, mover, stored)


async def test_a_move_waits_for_an_editors_judged_dependents_change(
    worker_db, monkeypatch
):
    waited, editor, mover, stored, parent_id = await _move_during_judged_write(
        worker_db, monkeypatch, "set_parent", _attach
    )
    assert waited, (editor, mover, stored)
    assert (editor, mover) == ("done", "done")
    assert stored is not None and stored[0] == "http" and stored[3] == parent_id


async def _retype_during_move(
    worker_db, victim_of, *, move: bool
) -> tuple[str, str, Any]:
    """An admin's retype to synthetic of a check on A, started while (with
    `move`) an editor's move of it to B is in flight; then an editor's write
    judged on the check's agent, started before the admin's transaction ends."""
    agent_a = await _agent(worker_db)
    agent_b = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_a)
    release = asyncio.Event()
    async with worker_db() as mover:
        if move:
            await _move(check_id, agent_b)(mover)
            await mover.flush()

        async def admin_change(s: AsyncSession) -> None:
            await _retype(check_id)(s)
            await s.flush()
            await release.wait()

        admin = _task(worker_db, admin_change)
        await asyncio.sleep(PAUSE)
        await mover.commit()
    await asyncio.sleep(PAUSE)
    victim = _task(worker_db, victim_of(check_id, agent_b if move else agent_a))
    await asyncio.sleep(PAUSE)
    release.set()
    admin_outcome = await asyncio.wait_for(admin, FINISH)
    victim_outcome = await asyncio.wait_for(victim, FINISH)
    return admin_outcome, victim_outcome, await _stored(worker_db, check_id)


@pytest.mark.parametrize("victim_of", [_edit, _delete, _agent_delete])
async def test_a_retype_of_a_check_being_moved_is_busy(worker_db, victim_of):
    admin, editor, stored = await _retype_during_move(worker_db, victim_of, move=True)
    assert admin == "refused:CheckBusyException", (admin, editor, stored)
    assert stored is None or stored[0] == "http", (admin, editor, stored)


@pytest.mark.parametrize("victim_of", [_edit, _delete, _agent_delete])
async def test_an_editor_waits_for_a_retype_on_the_same_agent(worker_db, victim_of):
    admin, editor, stored = await _retype_during_move(worker_db, victim_of, move=False)
    assert admin == "done", admin
    assert editor.startswith("refused"), (editor, stored)
    assert stored is not None and (stored[0], stored[2]) == ("synthetic", 60)
