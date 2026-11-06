"""A run's counts add to the row as it stands when the run ends.

A run reads the job, then runs for as long as the job takes. Another run past its
expired lease can count in that time, so a run must not write back what it read
when it began.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from app.models.enum_model import SchedulerJobCategory, SchedulerTriggerType
from app.models.scheduler_model import JobConfiguration
from app.services.core.scheduler_core_service import SchedulerCoreService
from sqlalchemy import select, update

pytestmark = pytest.mark.integration

KEY = "counted_job"


async def _seed(maker) -> None:
    async with maker() as s:
        s.add(
            JobConfiguration(
                job_key=KEY,
                function_name="counted",
                display_name="Counted",
                category=SchedulerJobCategory.MONITORING,
                trigger_type=SchedulerTriggerType.INTERVAL,
                interval_seconds=60,
                enabled=True,
                total_runs=10,
                failed_runs=10,
                retry_count=3,
                retry_limit=100,
            )
        )
        await s.commit()


async def _another_run_counts(maker) -> None:
    async with maker() as s:
        await s.execute(
            update(JobConfiguration)
            .where(JobConfiguration.job_key == KEY)
            .values(
                total_runs=JobConfiguration.total_runs + 5,
                failed_runs=JobConfiguration.failed_runs + 5,
                retry_count=JobConfiguration.retry_count + 5,
            )
        )
        await s.commit()


async def _stored(maker) -> JobConfiguration:
    async with maker() as s:
        return (
            await s.execute(
                select(JobConfiguration).where(JobConfiguration.job_key == KEY)
            )
        ).scalar_one()


def _service(maker, *, fails: bool) -> SchedulerCoreService:
    async def counted(**_: Any) -> dict[str, int]:
        await _another_run_counts(maker)
        if fails:
            raise RuntimeError("the job failed")
        return {"errors": 0}

    service = SchedulerCoreService()
    service.job_functions["counted"] = counted
    return service


async def _lease(maker) -> Any:
    token = uuid4()
    async with maker() as s:
        await s.execute(
            update(JobConfiguration)
            .where(JobConfiguration.job_key == KEY)
            .values(lease_token=token)
        )
        await s.commit()
    return token


async def test_a_scheduled_success_keeps_a_count_made_during_the_run(worker_db) -> None:
    await _seed(worker_db)
    token = await _lease(worker_db)

    await _service(worker_db, fails=False)._execute_job(KEY, token)

    assert (await _stored(worker_db)).total_runs == 16


async def test_a_scheduled_failure_keeps_a_count_made_during_the_run(worker_db) -> None:
    await _seed(worker_db)
    token = await _lease(worker_db)

    await _service(worker_db, fails=True)._execute_job(KEY, token)

    stored = await _stored(worker_db)
    assert stored.failed_runs == 16
    assert stored.retry_count == 9


async def test_a_manual_success_keeps_a_count_made_during_the_run(worker_db) -> None:
    await _seed(worker_db)

    await _service(worker_db, fails=False).execute_job_synchronously(KEY)

    assert (await _stored(worker_db)).total_runs == 16


async def test_a_manual_failure_keeps_a_count_made_during_the_run(worker_db) -> None:
    await _seed(worker_db)

    await _service(worker_db, fails=True).execute_job_synchronously(KEY)

    assert (await _stored(worker_db)).failed_runs == 16


async def test_a_job_disabled_during_a_failing_run_is_judged_on_the_stored_row(
    worker_db,
) -> None:
    await _seed(worker_db)
    async with worker_db() as s:
        await s.execute(
            update(JobConfiguration)
            .where(JobConfiguration.job_key == KEY)
            .values(retry_limit=9)
        )
        await s.commit()
    token = await _lease(worker_db)

    # 3 stored, 5 from the other run, 1 for this one: 9 is at the limit, not past it.
    await _service(worker_db, fails=True)._execute_job(KEY, token)

    stored = await _stored(worker_db)
    assert stored.retry_count == 9
    assert stored.enabled is True


LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


async def _due(maker, **overrides: Any) -> None:
    await _seed(maker)
    async with maker() as s:
        await s.execute(
            update(JobConfiguration)
            .where(JobConfiguration.job_key == KEY)
            .values(next_run_at=LONG_AGO, **overrides)
        )
        await s.commit()


async def test_a_scheduled_success_moves_the_next_run_on(worker_db) -> None:
    await _due(worker_db)
    token = await _lease(worker_db)

    await _service(worker_db, fails=False)._execute_job(KEY, token)

    stored = await _stored(worker_db)
    assert stored.next_run_at > datetime.now(UTC) + timedelta(seconds=30)
    assert stored.lease_token is None


async def test_a_cron_success_moves_the_next_run_on(worker_db) -> None:
    await _due(
        worker_db,
        trigger_type=SchedulerTriggerType.CRON,
        interval_seconds=None,
        cron_expression="0 3 * * *",
    )
    token = await _lease(worker_db)

    await _service(worker_db, fails=False)._execute_job(KEY, token)

    assert (await _stored(worker_db)).next_run_at > datetime.now(UTC)


async def test_a_manual_success_moves_the_next_run_on_and_says_so(worker_db) -> None:
    await _due(worker_db)

    out = await _service(worker_db, fails=False).execute_job_synchronously(KEY)

    stored = await _stored(worker_db)
    assert stored.next_run_at > datetime.now(UTC) + timedelta(seconds=30)
    assert out.job.next_run_at == stored.next_run_at


async def test_a_finished_job_is_not_due_again(worker_db) -> None:
    await _due(worker_db)
    runs: list[int] = []

    async def counted(**_: Any) -> dict[str, int]:
        runs.append(1)
        return {"errors": 0}

    service = SchedulerCoreService()
    service.job_functions["counted"] = counted

    for _ in range(3):
        await service._process_due_jobs()
        await asyncio.gather(*service._dispatched)

    assert len(runs) == 1


async def test_a_failure_moves_the_next_run_to_its_backoff(worker_db) -> None:
    await _due(worker_db)
    token = await _lease(worker_db)

    await _service(worker_db, fails=True)._execute_job(KEY, token)

    assert (await _stored(worker_db)).next_run_at > datetime.now(UTC)
