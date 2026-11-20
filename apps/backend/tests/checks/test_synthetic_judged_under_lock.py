"""An editor's change to a check, or to an agent's checks, is judged on the
checks as committed with no write making one synthetic in flight: a check an
admin is making synthetic at that moment (created, retyped, not yet committed)
is waited for and judged as synthetic, so the editor is refused as for a
committed one.

Each race holds the admin's write open (flushed, not committed) while the
editor's change starts, then commits it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from app.core.exceptions import AuthorizationException
from app.models.check_model import Check
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# How long the admin's write is held open after the editor's change starts.
HOLD_SECONDS = 1
# How long the editor's change is given once the admin's write commits.
FINISH_SECONDS = 10

TARGET = "https://127.0.0.1/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)
REFUSED = "synthetic checks require admin privileges"


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


async def _checks(worker_db, agent_id: UUID) -> list[tuple[str, str, bool, int]]:
    async with worker_db() as s:
        rows = await s.execute(
            select(
                Check.display_name,
                Check.check_type,
                Check.enabled,
                Check.interval_seconds,
            ).where(Check.agent_id == agent_id)
        )
        return [tuple(row) for row in rows.all()]


async def _retype_to_synthetic(admin: AsyncSession, check_id: UUID) -> None:
    await CheckCoreService.update_check(
        admin,
        check_id,
        CheckUpdate(check_type="synthetic", script_code=SCRIPT, target=TARGET),
        actor_is_admin=True,
    )
    await admin.flush()


async def _race(
    worker_db,
    admin_write: Callable[[AsyncSession], Awaitable[None]],
    editor_change: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    """Hold `admin_write` open while `editor_change` starts, then commit it.

    Returns what the editor's change returned and committed, or "refused" when
    it raised AuthorizationException (rolled back)."""

    async def editor() -> Any:
        async with worker_db() as s:
            try:
                outcome = await editor_change(s)
            except AuthorizationException:
                await s.rollback()
                return "refused"
            await s.commit()
            return outcome

    async with worker_db() as admin:
        await admin_write(admin)
        task = asyncio.create_task(editor())
        await asyncio.sleep(HOLD_SECONDS)
        await admin.commit()
    return await asyncio.wait_for(task, FINISH_SECONDS)


async def _delete_agent(s: AsyncSession, agent_id: UUID) -> str:
    await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=False)
    return "deleted"


async def test_an_editors_agent_delete_waits_for_a_synthetic_check_being_created(
    worker_db,
):
    agent_id = await _agent(worker_db)

    async def create(admin: AsyncSession) -> None:
        await CheckCoreService.create_check(
            admin,
            agent_id,
            CheckCreate(
                display_name="browse",
                check_type="synthetic",
                target=TARGET,
                script_code=SCRIPT,
            ),
            actor_is_admin=True,
        )
        await admin.flush()

    outcome = await _race(worker_db, create, lambda s: _delete_agent(s, agent_id))

    assert outcome == "refused"
    assert [c[:2] for c in await _checks(worker_db, agent_id)] == [
        ("browse", "synthetic")
    ]


async def test_an_editors_agent_delete_waits_for_a_check_being_retyped_synthetic(
    worker_db,
):
    agent_id = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_id)

    outcome = await _race(
        worker_db,
        lambda admin: _retype_to_synthetic(admin, check_id),
        lambda s: _delete_agent(s, agent_id),
    )

    assert outcome == "refused"
    assert [c[:2] for c in await _checks(worker_db, agent_id)] == [
        ("browse", "synthetic")
    ]


async def test_an_editors_agent_delete_of_plain_checks_still_deletes(worker_db):
    agent_id = await _agent(worker_db)
    await _http_check(worker_db, agent_id)

    async with worker_db() as s:
        await AgentCoreService.delete_agent(s, agent_id, actor_is_admin=False)
        await s.commit()

    assert await _checks(worker_db, agent_id) == []


async def test_an_editors_check_edit_waits_for_a_retype_to_synthetic(worker_db):
    agent_id = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_id)

    async def edit(s: AsyncSession) -> str:
        await CheckCoreService.update_check(
            s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
        )
        await s.flush()
        return "edited"

    outcome = await _race(
        worker_db, lambda admin: _retype_to_synthetic(admin, check_id), edit
    )

    assert outcome == "refused"
    assert await _checks(worker_db, agent_id) == [("browse", "synthetic", True, 60)]


async def test_an_editors_check_delete_waits_for_a_retype_to_synthetic(worker_db):
    agent_id = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_id)

    async def delete(s: AsyncSession) -> str:
        await CheckCoreService.delete_check(s, check_id, actor_is_admin=False)
        return "deleted"

    outcome = await _race(
        worker_db, lambda admin: _retype_to_synthetic(admin, check_id), delete
    )

    assert outcome == "refused"
    assert [c[:2] for c in await _checks(worker_db, agent_id)] == [
        ("browse", "synthetic")
    ]


async def test_an_editors_bulk_delete_waits_for_a_retype_to_synthetic(worker_db):
    agent_id = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_id)

    outcome = await _race(
        worker_db,
        lambda admin: _retype_to_synthetic(admin, check_id),
        lambda s: CheckCoreService.bulk_delete(s, [check_id], actor_is_admin=False),
    )

    assert outcome["success_count"] == 0, outcome
    assert any(REFUSED in error for error in outcome["errors"]), outcome
    assert [c[:2] for c in await _checks(worker_db, agent_id)] == [
        ("browse", "synthetic")
    ]


async def test_an_editors_bulk_disable_waits_for_a_retype_to_synthetic(worker_db):
    agent_id = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_id)

    outcome = await _race(
        worker_db,
        lambda admin: _retype_to_synthetic(admin, check_id),
        lambda s: CheckCoreService.bulk_set_enabled(
            s, [check_id], enabled=False, actor_is_admin=False
        ),
    )

    assert outcome["success_count"] == 0, outcome
    assert any(REFUSED in error for error in outcome["errors"]), outcome
    assert await _checks(worker_db, agent_id) == [("browse", "synthetic", True, 60)]


async def test_an_editors_dependents_change_waits_for_a_retype_to_synthetic(
    worker_db,
):
    agent_id = await _agent(worker_db)
    child_id = await _http_check(worker_db, agent_id)
    async with worker_db() as s:
        parent = make_check(agent_id=agent_id, display_name="parent")
        s.add(parent)
        await s.commit()
        parent_id = parent.id

    async def attach(s: AsyncSession) -> tuple[int, int]:
        return await CheckDependencyCoreService.set_dependents(
            s, parent_id, [child_id], actor_is_admin=False
        )

    outcome = await _race(
        worker_db, lambda admin: _retype_to_synthetic(admin, child_id), attach
    )

    assert outcome == "refused"
    async with worker_db() as s:
        depends_on = (
            await s.execute(
                select(Check.depends_on_check_id).where(Check.id == child_id)
            )
        ).scalar_one()
    assert depends_on is None


async def test_an_editors_bulk_modify_waits_for_a_retype_to_synthetic(worker_db):
    agent_id = await _agent(worker_db)
    check_id = await _http_check(worker_db, agent_id)

    async def modify(s: AsyncSession) -> dict[str, Any]:
        outcome = await CheckCoreService.bulk_modify(
            s, [check_id], CheckUpdate(interval_seconds=600), actor_is_admin=False
        )
        await s.flush()
        return outcome

    outcome = await _race(
        worker_db, lambda admin: _retype_to_synthetic(admin, check_id), modify
    )

    assert outcome["success_count"] == 0, outcome
    assert await _checks(worker_db, agent_id) == [("browse", "synthetic", True, 60)]
