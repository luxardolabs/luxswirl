"""The database maintenance cycle against real TimescaleDB.

Two defects these pin:

* The continuous-aggregate refresh ran through an ordinary session, i.e. inside a
  transaction block, which TimescaleDB refuses (`refresh_continuous_aggregate()
  cannot run inside a transaction block`): that step failed on every cycle.
* VACUUM borrowed a connection from the app engine, whose 5-second
  `statement_timeout` guards the web path, so a VACUUM FULL of any real table
  was cancelled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from app.crud.database_maintenance_crud import (
    CONTINUOUS_AGGREGATES,
    DatabaseMaintenanceCRUD,
)
from app.db import database as _database
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import CheckArtifactType
from app.schemas.database_maintenance_schema import TableBloat
from app.services.core.database_maintenance_core_service import (
    DatabaseMaintenanceCoreService,
    vacuum_kind,
)
from fixtures._db_url import TEST_DATABASE_URL
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

_OLD = datetime(2000, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def app_engine(worker_db) -> AsyncIterator[AsyncEngine]:
    """The global app engine, bound to the test DB with the app's web-path
    timeout (shortened to 500 ms so a test can outlast it)."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"server_settings": {"statement_timeout": "500"}},
    )
    saved = _database.engine
    _database.engine = engine
    try:
        yield engine
    finally:
        _database.engine = saved
        await engine.dispose()


def _bloat(live: int, dead: int) -> TableBloat:
    return TableBloat(
        table="checks", size_bytes=0, row_count=live, live_tuples=live, dead_tuples=dead
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("live", "dead", "expected"),
    [
        (0, 0, None),
        (100, 5, None),
        (100, 12, False),  # 10.7% bloat
        (1_000_000, 1_001, False),  # 0.1% but over 1,000 dead
        (100, 101, True),  # 50.2% bloat
        (10_000_000, 10_001, True),  # 0.1% but over 10,000 dead
    ],
)
def test_vacuum_kind_thresholds(live: int, dead: int, expected: bool | None) -> None:
    assert vacuum_kind(_bloat(live, dead)) is expected


@pytest.mark.unit
def test_bloat_of_an_empty_table_is_zero() -> None:
    assert _bloat(0, 0).bloat_percentage == 0.0


async def test_an_unknown_table_is_refused_before_any_sql(app_engine) -> None:
    with pytest.raises(ValueError, match="Invalid table name"):
        await DatabaseMaintenanceCRUD.vacuum("users; DROP TABLE users", full=False)


async def test_a_refresh_inside_a_transaction_is_what_failed(worker_db) -> None:
    """The old shape, kept as the reason the CRUD uses its own connection."""
    async with worker_db() as s:
        with pytest.raises(DBAPIError, match="cannot run inside a transaction block"):
            await s.execute(
                text(
                    "CALL refresh_continuous_aggregate('check_results_5min', NULL, NULL)"
                )
            )


async def test_every_continuous_aggregate_refreshes(app_engine) -> None:
    for name in CONTINUOUS_AGGREGATES:
        await DatabaseMaintenanceCRUD.refresh_continuous_aggregate(name)


async def test_vacuum_outlasts_the_web_timeout_then_restores_it(worker_db) -> None:
    """A VACUUM FULL of ~200k result rows cannot finish inside a 100 ms app
    timeout, so it succeeding proves its connection lifted the timeout, and the
    pooled connection reading 100 ms again proves the lift was reset."""
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id)
        s.add(check)
        await s.flush()
        # The seed runs on the app's own sessions, whose 5 s statement timeout
        # a 200k-row insert outlasts; it is setup, not what is under test.
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        await s.execute(
            text(
                "INSERT INTO check_results "
                "(id, timestamp, agent_id, check_id, success, latency_ms, error) "
                "SELECT gen_random_uuid(), now() - g * interval '1 second', :a, :c, "
                "true, 1.0, repeat('x', 200) FROM generate_series(1, 200000) g"
            ),
            {"a": agent.id, "c": check.id},
        )
        await s.commit()

    engine = create_async_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"statement_timeout": "100"}},
    )
    saved = _database.engine
    _database.engine = engine
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            with pytest.raises(DBAPIError, match="statement timeout"):
                await conn.execute(text("VACUUM FULL check_results"))

        result = await DatabaseMaintenanceCoreService.vacuum("check_results", full=True)
        assert result.error is None, result.error

        async with engine.connect() as conn:  # the same pooled connection
            timeout = (await conn.execute(text("SHOW statement_timeout"))).scalar()
        assert timeout == "100ms"
    finally:
        _database.engine = saved
        await engine.dispose()


async def test_vacuum_reports_sizes(app_engine) -> None:
    result = await DatabaseMaintenanceCoreService.vacuum("jobs", full=True)
    assert result.error is None
    assert result.size_before_bytes is not None
    assert result.size_after_bytes is not None


async def test_measure_bloat_counts_the_rows(worker_db, app_engine) -> None:
    async with worker_db() as s:
        s.add(make_agent(agent_name="a1"))
        s.add(make_agent(agent_name="a2"))
        await s.commit()

    bloat = await DatabaseMaintenanceCoreService.measure_bloat("agents")

    assert bloat.table == "agents"
    assert bloat.row_count == 2
    assert bloat.size_bytes > 0


async def test_artifact_retention_deletes_old_keeps_recent(
    worker_db, app_engine
) -> None:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id)
        s.add(check)
        await s.flush()
        for created in (_OLD, datetime.now(UTC)):
            s.add(
                CheckArtifact(
                    id=uuid4(),
                    created_at=created,
                    check_id=check.id,
                    check_result_id=uuid4(),
                    check_result_timestamp=created,
                    artifact_type=CheckArtifactType.SCREENSHOT,
                    content_type="image/png",
                    filename="shot.png",
                    size_bytes=1,
                    data=b"x",
                )
            )
        await s.commit()

    assert await DatabaseMaintenanceCoreService.enforce_artifact_retention() == 1

    async with worker_db() as s:
        kept = (await s.execute(select(CheckArtifact.created_at))).scalars().all()
    assert len(kept) == 1 and kept[0] > _OLD


async def test_a_full_cycle_runs_every_step_clean(worker_db, app_engine) -> None:
    report = await DatabaseMaintenanceCoreService.run_cycle()

    assert report.failures == []
    assert report.aggregates_refreshed == list(CONTINUOUS_AGGREGATES)
    assert {b.table for b in report.bloat} >= {"check_results", "agents"}
