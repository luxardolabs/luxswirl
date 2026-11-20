"""A write judging a check under its agent's synthetic guard judges the agent
the check is on when the guard is held, not the one it was on before.

An editor's single delete or edit that waits for the guard of the agent it
read the check on, while the check moves to another agent and an admin retypes
it to synthetic there, is answered busy. An import that overwrites, by name, a
check an editor moved off the import's agent mid-run leaves it alone and
reports that entry busy, so it never retypes a check on an agent whose guard
it does not hold.
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
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

TARGET = "https://127.0.0.1/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _http_check(worker_db, agent_id: UUID) -> UUID:
    async with worker_db() as s:
        check = make_check(
            agent_id=agent_id, display_name="browse", check_type="http", target=TARGET
        )
        s.add(check)
        await s.commit()
        return check.id


async def _stored(worker_db, check_id: UUID) -> tuple[Any, ...] | None:
    async with worker_db() as s:
        row = (
            await s.execute(
                select(Check.check_type, Check.agent_id, Check.interval_seconds).where(
                    Check.id == check_id
                )
            )
        ).one_or_none()
        return tuple(row) if row else None


async def _move(worker_db, check_id: UUID, to_agent: UUID) -> None:
    """An editor's bulk move of a plain check (takes no synthetic guard)."""
    async with worker_db() as s:
        out = await CheckCoreService.bulk_modify(
            s, [check_id], CheckUpdate(), new_agent_id=to_agent, actor_is_admin=False
        )
        assert out["success_count"] == 1, out
        await s.commit()


async def _retype_to_synthetic(admin: AsyncSession, check_id: UUID) -> None:
    await CheckCoreService.update_check(
        admin,
        check_id,
        CheckUpdate(check_type="synthetic", script_code=SCRIPT, target=TARGET),
        actor_is_admin=True,
    )
    await admin.flush()


def _editor(
    worker_db, change: Callable[[AsyncSession], Awaitable[None]]
) -> asyncio.Task[str]:
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


def _delete(check_id: UUID) -> Callable[[AsyncSession], Awaitable[None]]:
    async def run(s: AsyncSession) -> None:
        await CheckCoreService.delete_check(s, check_id, actor_is_admin=False)

    return run


def _edit(check_id: UUID) -> Callable[[AsyncSession], Awaitable[None]]:
    async def run(s: AsyncSession) -> None:
        await CheckCoreService.update_check(
            s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
        )

    return run


async def _race_move_while_editor_waits(worker_db, change_of) -> tuple[str, Any]:
    agent_a = await _agent(worker_db)
    agent_b = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_a)
    async with worker_db() as holder:
        # An admin's write on agent A in flight (e.g. a synthetic create):
        # agent A's synthetic guard is held.
        await CheckCRUD.hold_synthetic_guard(holder, {agent_a})
        editor = _editor(worker_db, change_of(check_id))
        await asyncio.sleep(1.0)  # the editor read the check on A, waits for A
        await _move(worker_db, check_id, agent_b)
        async with worker_db() as admin:
            await _retype_to_synthetic(admin, check_id)  # holds B's guard + row
            await holder.commit()  # the editor takes A's guard, reads committed
            await asyncio.sleep(1.0)
            await admin.commit()
        outcome = await asyncio.wait_for(editor, 15)
    return outcome, await _stored(worker_db, check_id)


async def _control_move_before_editor(worker_db, change_of) -> tuple[str, Any]:
    agent_a = await _agent(worker_db)
    agent_b = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_a)
    await _move(worker_db, check_id, agent_b)
    async with worker_db() as admin:
        await _retype_to_synthetic(admin, check_id)
        editor = _editor(worker_db, change_of(check_id))
        await asyncio.sleep(1.0)
        await admin.commit()
    outcome = await asyncio.wait_for(editor, 15)
    return outcome, await _stored(worker_db, check_id)


async def test_an_editors_delete_of_a_check_moved_while_it_waited_is_busy(worker_db):
    outcome, stored = await _race_move_while_editor_waits(worker_db, _delete)
    assert outcome == "refused:CheckBusyException", (outcome, stored)
    assert stored is not None and stored[0] == "synthetic", (outcome, stored)


async def test_an_editors_edit_of_a_check_moved_while_it_waited_is_busy(worker_db):
    outcome, stored = await _race_move_while_editor_waits(worker_db, _edit)
    assert outcome == "refused:CheckBusyException", (outcome, stored)
    assert stored is not None and stored[2] == 60, (outcome, stored)


async def test_an_editors_delete_after_the_move_is_refused(worker_db):
    outcome, stored = await _control_move_before_editor(worker_db, _delete)
    assert outcome.startswith("refused"), (outcome, stored)
    assert stored is not None and stored[0] == "synthetic", (outcome, stored)


async def test_an_editors_edit_after_the_move_is_refused(worker_db):
    outcome, stored = await _control_move_before_editor(worker_db, _edit)
    assert outcome.startswith("refused"), (outcome, stored)
    assert stored is not None and stored[2] == 60, (outcome, stored)


def _synthetic_entry() -> dict[str, Any]:
    return {
        "name": "browse",
        "check_type": "synthetic",
        "target": TARGET,
        "script_code": SCRIPT,
    }


async def _import_synthetic(
    worker_db, agent_id: UUID, monkeypatch, *, move_to: UUID | None, check_id: UUID
) -> dict[str, Any]:
    """An admin's overwriting import of a synthetic entry named as the check;
    with `move_to`, an editor's bulk move of the check commits mid-import,
    after the import read the agent's checks."""
    if move_to is not None:
        original = CheckTransferCoreService._import_one

        async def import_one(*args: Any, **kwargs: Any) -> str:
            await _move(worker_db, check_id, move_to)
            return await original(*args, **kwargs)

        monkeypatch.setattr(CheckTransferCoreService, "_import_one", import_one)
    async with worker_db() as admin:
        result = await CheckTransferCoreService.bulk_import_checks(
            admin,
            agent_id,
            [_synthetic_entry()],
            overwrite=True,
            actor_is_admin=True,
        )
        await admin.commit()
    return result


async def test_an_import_leaves_a_check_moved_off_its_agent_unchanged(
    worker_db, monkeypatch
):
    agent_a = await _agent(worker_db)
    agent_c = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_a)

    result = await _import_synthetic(
        worker_db, agent_a, monkeypatch, move_to=agent_c, check_id=check_id
    )

    assert result["updated"] == 0 and len(result["errors"]) == 1, result
    assert await _stored(worker_db, check_id) == ("http", agent_c, 60)


async def test_an_import_retypes_a_check_still_on_its_agent(worker_db, monkeypatch):
    agent_a = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_a)

    result = await _import_synthetic(
        worker_db, agent_a, monkeypatch, move_to=None, check_id=check_id
    )

    assert result["updated"] == 1 and result["errors"] == [], result
    assert await _stored(worker_db, check_id) == ("synthetic", agent_a, 60)
