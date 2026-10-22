"""The detail panel's history is bucketed at the width its time range asks for.

The bucket width was spliced into the SQL text (`time_bucket('{interval}', …)`).
It is now a bound interval parameter, so these pin that each range still lands
on its width: 1-minute buckets up to 4h, 30-minute buckets up to 72h.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.datetime_utils import utc_now
from app.crud.check_result_crud import CheckResultCRUD
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _check_with_results(db: AsyncSession, minutes_ago: list[int]):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    now = utc_now()
    for m in minutes_ago:
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now - timedelta(minutes=m, seconds=7),
            )
        )
    await db.flush()
    return check


async def test_a_short_range_buckets_by_the_minute(db: AsyncSession) -> None:
    check = await _check_with_results(db, [1, 3, 5])

    rows = await CheckResultCRUD.get_bucketed_history(db, check.id, hours=4)

    assert len(rows) == 3
    assert all(r.bucket_time.second == 0 for r in rows)


async def test_a_multi_day_range_buckets_by_the_half_hour(db: AsyncSession) -> None:
    check = await _check_with_results(db, [1, 3, 5, 100, 200])

    rows = await CheckResultCRUD.get_bucketed_history(db, check.id, hours=48)

    assert rows
    assert all(
        r.bucket_time.minute in (0, 30) and r.bucket_time.second == 0 for r in rows
    )
    # Five results spread over ~3h fall into far fewer half-hour buckets.
    assert len(rows) < 5
