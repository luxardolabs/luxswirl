"""A delete of a parent check, by a check delete, a bulk delete or an agent
delete, does not hold the children it only clears (on another agent) against
their own reports and alert decisions, which take FOR KEY SHARE on the check
(`CheckCRUD.key_share_existing`); a bulk delete of many ids is not refused by
its lock; and an edit whose check changes under it is saved on, or refused by,
the row as stored."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.exceptions import AuthorizationException
from app.crud.check_crud import CheckCRUD
from app.models.check_model import Check
from app.schemas.check_schema import CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text, update

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


async def _parent_and_foreign_child(worker_db) -> tuple[UUID, UUID, UUID]:
    """A parent on agent A; its child on agent B. Returns (A, parent, child)."""
    agent_a = await _agent(worker_db)
    agent_b = await _agent(worker_db)
    parent = await _check(worker_db, agent_a, display_name="parent")
    child = await _check(
        worker_db, agent_b, display_name="child", depends_on_check_id=parent
    )
    return agent_a, parent, child


async def _report_lock(worker_db, check_id: UUID) -> str:
    """What a report (and the alert decision worker) takes on the check its
    result references, given 1 s: the outcome."""
    async with worker_db() as s:
        try:
            await s.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            present = await CheckCRUD.key_share_existing(s, [check_id])
            await s.rollback()
            return "stored" if check_id in present else "dropped"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return f"blocked:{str(getattr(e, 'orig', e)).splitlines()[0][:160]}"


# The change: each delete in flight (its cascade still running), then a report
# for the child on the other agent.


async def test_agent_delete_in_flight_leaves_a_foreign_childs_reports_free(
    worker_db,
):
    agent_a, _, child = await _parent_and_foreign_child(worker_db)
    async with worker_db() as s:
        await AgentCoreService.delete_agent(s, agent_a, actor_is_admin=True)
        outcome = await _report_lock(worker_db, child)
        await s.rollback()
    assert outcome == "stored", outcome


async def test_check_delete_in_flight_leaves_its_childs_reports_free(worker_db):
    _, parent, child = await _parent_and_foreign_child(worker_db)
    async with worker_db() as s:
        await CheckCoreService.delete_check(s, parent, actor_is_admin=True)
        outcome = await _report_lock(worker_db, child)
        await s.rollback()
    assert outcome == "stored", outcome


async def test_bulk_delete_in_flight_leaves_a_foreign_childs_reports_free(
    worker_db,
):
    _, parent, child = await _parent_and_foreign_child(worker_db)
    async with worker_db() as s:
        out = await CheckCoreService.bulk_delete(s, [parent], actor_is_admin=True)
        assert out["success_count"] == 1, out
        outcome = await _report_lock(worker_db, child)
        await s.rollback()
    assert outcome == "stored", outcome


# A bulk delete's lock names its ids twice (the removed checks, and the
# subquery selecting their children); the driver binds each once.

MANY = 17_000


async def test_bulk_delete_of_many_ids_is_not_refused_by_its_lock(worker_db):
    ids = [uuid4() for _ in range(MANY)]
    async with worker_db() as s:
        out = await CheckCoreService.bulk_delete(s, ids, actor_is_admin=True)
        await s.rollback()
    # Unknown ids are reported as not found: the lock statement ran.
    assert out["errors"] == [f"{MANY} check(s) not found"], str(out["errors"])[:400]


# An edit whose check changes between its first read and its hold: the hold
# is released through a savepoint and taken again on the new row.

SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)


def _change_meanwhile(monkeypatch, worker_db, check_id: UUID, **values: Any):
    """The first time the edit vets its endpoints, another transaction commits
    `values` onto the check."""
    real = SettingsCoreService.get_network_protection
    done = {"n": 0}

    async def racing(db):
        if done["n"] == 0:
            done["n"] = 1
            async with worker_db() as other:
                await other.execute(
                    update(Check).where(Check.id == check_id).values(**values)
                )
                await other.commit()
        return await real(db)

    monkeypatch.setattr(
        SettingsCoreService, "get_network_protection", staticmethod(racing)
    )


async def _stored(worker_db, check_id: UUID) -> tuple[Any, ...]:
    async with worker_db() as s:
        row = (
            await s.execute(
                select(Check.check_type, Check.target, Check.interval_seconds).where(
                    Check.id == check_id
                )
            )
        ).one()
        return tuple(row)


async def test_an_edit_whose_target_moves_meanwhile_is_saved_on_the_new_row(
    worker_db, monkeypatch
):
    agent = await _agent(worker_db)
    check_id = await _check(
        worker_db, agent, check_type="http", target="https://127.0.0.1/a"
    )
    _change_meanwhile(monkeypatch, worker_db, check_id, target="https://127.0.0.1/b")
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(
                s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
            )
            await s.commit()
            outcome = "edited"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            outcome = f"error:{type(e).__name__}:{e}"[:300]
    assert outcome == "edited", outcome
    assert await _stored(worker_db, check_id) == ("http", "https://127.0.0.1/b", 600)


async def test_an_editors_edit_of_a_check_retyped_synthetic_meanwhile_is_refused(
    worker_db, monkeypatch
):
    agent = await _agent(worker_db)
    check_id = await _check(
        worker_db, agent, check_type="http", target="https://127.0.0.1/a"
    )
    _change_meanwhile(
        monkeypatch,
        worker_db,
        check_id,
        check_type="synthetic",
        script_code=SCRIPT,
        target="https://127.0.0.1/",
    )
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(
                s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
            )
            await s.commit()
            outcome = "edited"
        except AuthorizationException:
            await s.rollback()
            outcome = "refused"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            outcome = f"error:{type(e).__name__}:{e}"[:300]
    assert outcome == "refused", outcome
    assert (await _stored(worker_db, check_id))[2] == 60
