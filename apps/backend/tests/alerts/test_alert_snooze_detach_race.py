"""A snooze or unsnooze racing a detach of the same alert-check pair.

The detach removes the pair after the snooze has read it: the snooze's write
waits on the detach, finds the pair gone, and answers that it is not found
(404), never an untyped database error.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.services.core.alert_core_service import AlertCoreService
from fixtures.factories import make_agent, make_alert, make_check
from sqlalchemy import delete, func, select, text

pytestmark = pytest.mark.integration


async def _mapped_pair(worker_db) -> tuple[UUID, UUID]:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id)
        alert = make_alert()
        s.add_all([check, alert])
        await s.flush()
        # Snoozed already, so an unsnooze has a change to write.
        s.add(
            AlertCheckMapping(
                alert_id=alert.id,
                check_id=check.id,
                is_enabled=True,
                snoozed_until=utc_now() + timedelta(minutes=5),
            )
        )
        await s.commit()
        return alert.id, check.id


async def _until_waiting_on(worker_db, pid: int) -> None:
    """Return once backend `pid` is waiting on a lock."""
    for _ in range(200):
        async with worker_db() as s:
            waiting = await s.scalar(
                select(func.count())
                .select_from(text("pg_locks"))
                .where(text("pid = :pid AND NOT granted").bindparams(pid=pid))
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the snooze never waited on the detach")


@pytest.mark.parametrize("snooze", [True, False], ids=["snooze", "unsnooze"])
async def test_a_detach_committed_while_the_snooze_waits_is_not_found(
    worker_db, snooze: bool
) -> None:
    alert_id, check_id = await _mapped_pair(worker_db)

    async def change() -> None:
        async with worker_db() as s:
            await s.execute(text("SET LOCAL lock_timeout = '10s'"))
            pid["snoozer"] = await s.scalar(select(func.pg_backend_pid()))
            ready.set()
            if snooze:
                await AlertCoreService.snooze_alert_check(s, alert_id, check_id, 15)
            else:
                await AlertCoreService.unsnooze_alert_check(s, alert_id, check_id)
            await s.commit()

    pid: dict[str, int] = {}
    ready = asyncio.Event()
    async with worker_db() as detacher:
        # The detach holds the pair, deleted but not yet committed.
        await detacher.execute(
            delete(AlertCheckMapping).where(
                AlertCheckMapping.alert_id == alert_id,
                AlertCheckMapping.check_id == check_id,
            )
        )
        task = asyncio.create_task(change())
        await ready.wait()
        await _until_waiting_on(worker_db, pid["snoozer"])
        await detacher.commit()

    with pytest.raises(NotFoundException):
        await task
    async with worker_db() as s:
        assert (
            await s.scalars(
                select(AlertCheckMapping).where(AlertCheckMapping.alert_id == alert_id)
            )
        ).all() == []
