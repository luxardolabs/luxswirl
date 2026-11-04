"""The test database runs no TimescaleDB job on its own.

`init_timescale` adds the app's policies to the test schema (a 30-day retention
on check_artifacts, retention on check_results and agent_metrics, compression,
continuous aggregate refreshes). With TimescaleDB's job scheduler running, each
policy fired about a second after the schema was built, while the first tests
were running: the check_artifacts retention dropped a `worker_db` test's
committed rows dated 45 and 61 days back between two of its statements, and a
policy holding its locks deadlocked the test-db-bound block's TRUNCATE, leaving
that test's rows for the next one. The test service starts Postgres with
`timescaledb.max_background_workers=0` (compose.yml, `timescaledb-test`), so
no scheduler runs for the suite's database; a test that wants a policy's effect
runs it itself with `CALL run_job(...)`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_no_timescaledb_scheduler_serves_the_test_database(
    db: AsyncSession,
) -> None:
    schedulers = (
        await db.execute(
            text(
                "SELECT pid, backend_type FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND backend_type ILIKE 'TimescaleDB%Scheduler%'"
            )
        )
    ).all()

    assert schedulers == []


async def test_the_app_policies_exist_and_have_never_run(db: AsyncSession) -> None:
    rows = (
        await db.execute(
            text(
                "SELECT j.proc_name, j.hypertable_name, coalesce(s.total_runs, 0) "
                "FROM timescaledb_information.jobs j "
                "LEFT JOIN timescaledb_information.job_stats s USING (job_id) "
                "WHERE j.hypertable_name = 'check_artifacts'"
            )
        )
    ).all()

    assert {(r[0], r[1]) for r in rows} == {
        ("policy_compression", "check_artifacts"),
        ("policy_retention", "check_artifacts"),
    }
    assert [r for r in rows if r[2] != 0] == []
