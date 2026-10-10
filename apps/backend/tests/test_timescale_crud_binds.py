"""TimescaleDB admin SQL binds every caller value; nothing is spliced.

The policy calls took a hypertable name and the growth queries a bucket width
by f-string, so a crafted value became SQL. These run each statement against
real TimescaleDB with bound values, and show a hostile name is refused as a
relation name rather than executed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.datetime_utils import utc_now
from app.crud.timescale_crud import TimescaleCRUD
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_retention_and_compression_policies_round_trip(db: AsyncSession) -> None:
    await TimescaleCRUD.remove_retention_policy(db, "agent_metrics")
    await TimescaleCRUD.add_retention_policy(db, "agent_metrics", 30)
    await TimescaleCRUD.remove_compression_policy(db, "check_artifacts")
    await TimescaleCRUD.add_compression_policy(db, "check_artifacts", 7)

    jobs = (
        await db.execute(
            text(
                "SELECT hypertable_name, proc_name FROM timescaledb_information.jobs "
                "WHERE hypertable_name IN ('agent_metrics', 'check_artifacts')"
            )
        )
    ).all()
    assert ("agent_metrics", "policy_retention") in jobs
    assert ("check_artifacts", "policy_compression") in jobs


async def test_a_hostile_table_name_is_a_missing_relation_not_sql(
    db: AsyncSession,
) -> None:
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await TimescaleCRUD.remove_retention_policy(
                db, "agents', if_exists => TRUE); DROP TABLE agents; --"
            )

    still_there = await db.execute(text("SELECT to_regclass('agents')"))
    assert still_there.scalar() == "agents"


async def test_growth_queries_run_with_bound_windows(db: AsyncSession) -> None:
    assert await TimescaleCRUD.get_growth_buckets(db, "1 hour", 24) == []
    assert await TimescaleCRUD.get_daily_growth(db, 7) == []


async def test_daily_growth_counts_per_day_inside_the_window(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    # Noon, so neither row of "today" can fall across midnight from the other.
    noon = utc_now().replace(hour=12, minute=0, second=0, microsecond=0)
    for ts in (
        noon,
        noon + timedelta(seconds=1),
        noon - timedelta(days=1),
        noon - timedelta(days=30),  # outside the 7-day window
    ):
        db.add(make_check_result(check_id=check.id, agent_id=agent.id, timestamp=ts))
    await db.flush()

    rows = await TimescaleCRUD.get_daily_growth(db, 7)

    assert rows == [(str(noon.date()), 2), (str((noon - timedelta(days=1)).date()), 1)]
