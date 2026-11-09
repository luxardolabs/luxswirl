"""Uptime counts read from the hourly aggregate equal the counts of the raw results.

A status page counts each check's results over 24 hours, 7, 30 and 90 days. Counted
from raw results, the 90-day window passed the 5 s statement timeout on a real
database and the page answered 500. `get_uptime_counts_bulk` reads whole hours from
`check_results_hourly` and only the edges raw; these tests hold it to the raw count,
with window starts mid-hour, results written after the aggregate was refreshed,
and a result written into an hour the aggregate already holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.core.datetime_utils import utc_now
from app.crud.check_result_crud import CheckResultCRUD, ResultCounts
from app.crud.database_maintenance_crud import DatabaseMaintenanceCRUD
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

WINDOWS = (24, 168, 720)


def _expected(stamps: list[tuple[datetime, bool]], hours: int) -> ResultCounts:
    since = utc_now() - timedelta(hours=hours)
    inside = [ok for at, ok in stamps if at >= since]
    return ResultCounts(len(inside), sum(inside))


async def _seed(maker: async_sessionmaker[AsyncSession], check_id, agent_id, stamps):
    async with maker() as db:
        db.add_all(
            make_check_result(
                check_id=check_id, agent_id=agent_id, timestamp=at, success=ok
            )
            for at, ok in stamps
        )
        await db.commit()


async def _refresh() -> None:
    await DatabaseMaintenanceCRUD.refresh_continuous_aggregate("check_results_hourly")


async def test_counts_from_the_aggregate_equal_the_raw_counts(worker_db) -> None:
    async with worker_db() as db:
        agent = make_agent()
        db.add(agent)
        await db.flush()
        often = make_check(agent_id=agent.id, display_name="often")
        seldom = make_check(agent_id=agent.id, display_name="seldom")
        db.add_all([often, seldom])
        await db.commit()

    now = utc_now()
    # Three minutes off every 20-minute mark, so no result sits on a window's edge.
    often_stamps = [
        (now - timedelta(minutes=20 * k + 3), k % 3 != 0) for k in range(30 * 3)
    ]
    seldom_stamps = [
        (now - timedelta(minutes=45 * k + 7), k % 5 != 0) for k in range(10 * 32)
    ]
    # A result just inside and just outside each window's start: the part-hour a
    # window opens with is read raw, so it must hold one, whatever the clock says.
    for hours in WINDOWS:
        start = now - timedelta(hours=hours)
        often_stamps += [
            (start + timedelta(seconds=90), False),
            (start - timedelta(seconds=90), False),
        ]
    await _seed(worker_db, often.id, agent.id, often_stamps)
    await _seed(worker_db, seldom.id, agent.id, seldom_stamps)
    await _refresh()

    # Written after the refresh: two in the last two hours, which are read raw.
    after = [(now - timedelta(minutes=9), False), (now - timedelta(minutes=71), True)]
    await _seed(worker_db, often.id, agent.id, after)
    often_stamps += after

    async with worker_db() as db:
        counts = await CheckResultCRUD.get_uptime_counts_bulk(
            db, [often.id, seldom.id], WINDOWS
        )

    for hours in WINDOWS:
        assert counts[often.id][hours] == _expected(often_stamps, hours), hours
        assert counts[seldom.id][hours] == _expected(seldom_stamps, hours), hours


async def test_a_result_written_into_an_aggregated_hour_counts_at_the_next_refresh(
    worker_db,
) -> None:
    async with worker_db() as db:
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.commit()

    now = utc_now()
    stamps = [(now - timedelta(hours=h, minutes=13), True) for h in range(3, 20)]
    await _seed(worker_db, check.id, agent.id, stamps)
    await _refresh()
    # An agent replaying a stored batch writes into an hour already aggregated.
    late = [(now - timedelta(hours=6, minutes=40), False)]
    await _seed(worker_db, check.id, agent.id, late)

    async with worker_db() as db:
        before = await CheckResultCRUD.get_uptime_counts_bulk(db, [check.id], (24,))
    assert before[check.id][24] == _expected(stamps, 24)

    await _refresh()
    async with worker_db() as db:
        after = await CheckResultCRUD.get_uptime_counts_bulk(db, [check.id], (24,))
    assert after[check.id][24] == _expected(stamps + late, 24)


async def test_the_24h_stats_read_the_same_counts(worker_db) -> None:
    async with worker_db() as db:
        agent = make_agent()
        db.add(agent)
        await db.flush()
        reporting = make_check(agent_id=agent.id, display_name="reporting")
        silent = make_check(agent_id=agent.id, display_name="silent")
        db.add_all([reporting, silent])
        await db.commit()

    now = utc_now()
    stamps = [(now - timedelta(minutes=30 * k + 4), k % 4 != 0) for k in range(60)]
    await _seed(worker_db, reporting.id, agent.id, stamps)
    await _refresh()

    async with worker_db() as db:
        day = await CheckResultCRUD.get_24h_stats_batch(db, [reporting.id, silent.id])

    assert day == {reporting.id: _expected(stamps, 24)}


async def test_no_checks_or_no_windows_reads_nothing(db: AsyncSession) -> None:
    assert await CheckResultCRUD.get_uptime_counts_bulk(db, [], WINDOWS) == {}
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    assert await CheckResultCRUD.get_uptime_counts_bulk(db, [check.id], ()) == {
        check.id: {}
    }
