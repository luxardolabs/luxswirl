"""A bulk enable or disable whose statement failed reports the failure as
anyone who reads the result may read it: without the statement's SQL.

The result goes straight back to the requester (an editor's request), so its
error is cut as a job's progress is (`MaintenanceJobCoreService.readable_error`).
Here the statement fails on a lock another transaction holds past the
request's statement timeout.
"""

import asyncio
from uuid import UUID

import pytest
from app.crud.check_crud import CheckCRUD
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_check

pytestmark = pytest.mark.integration


async def _check(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id, display_name="site")
        s.add(check)
        await s.commit()
        return check.id


async def test_a_bulk_disable_cut_off_by_a_held_row_reports_no_sql(worker_db) -> None:
    check_id = await _check(worker_db)
    async with worker_db() as holder:
        await CheckCRUD.get_locked_by_ids(holder, [check_id], for_delete=True)
        async with worker_db() as editor:
            outcome = await CheckCoreService.bulk_set_enabled(
                editor, [check_id], enabled=False, actor_is_admin=False
            )
            await editor.rollback()
        await holder.rollback()
    await asyncio.sleep(0)

    assert outcome["success_count"] == 0, outcome
    assert outcome["failure_count"] == 1, outcome
    [error] = outcome["errors"]
    assert "statement timeout" in error, outcome
    assert "[SQL:" not in error, outcome
    assert "SELECT" not in error and "UPDATE" not in error, outcome
