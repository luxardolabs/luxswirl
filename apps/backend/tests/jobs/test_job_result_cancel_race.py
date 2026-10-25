"""A job's result and an operator's cancel landing together: the first one wins.

Each finishes the job in one statement that only matches an unfinished job,
so the second waits for the first, then reads its committed state and leaves
the job as the first left it.
"""

from __future__ import annotations

import asyncio

import pytest
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.job_schema import JobResultSubmit
from app.services.core.job_api_core_service import JobApiCoreService
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import make_agent
from sqlalchemy import select

pytestmark = pytest.mark.integration

_RESULT = JobResultSubmit(status=JobStatus.COMPLETED, result={"hosts": 1})


async def _seed(maker) -> Job:
    async with maker() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        job = Job(
            job_type=JobType.NETWORK_SCAN,
            agent_id=agent.id,
            params={"subnet": "10.0.0.0/24"},
            status=JobStatus.RUNNING,
        )
        s.add(job)
        await s.commit()
        return job


async def _stored(maker, job_id) -> Job:
    async with maker() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def _race(maker, first, second):
    """Run `first`, start `second` before `first` commits, then commit both."""
    async with maker() as s1, maker() as s2:
        await first(s1)
        pending = asyncio.create_task(second(s2))
        await asyncio.sleep(0.3)
        await s1.commit()
        outcome = await pending
        await s2.commit()
    return outcome


async def test_a_cancel_landing_after_the_result_leaves_the_job_completed(
    worker_db,
) -> None:
    job = await _seed(worker_db)

    cancelled = await _race(
        worker_db,
        lambda s: JobCoreService.submit_job_result(s, job.id, _RESULT),
        lambda s: JobCoreService.cancel_job(s, job.id),
    )

    assert cancelled is None
    stored = await _stored(worker_db, job.id)
    assert stored.status == JobStatus.COMPLETED
    assert stored.result is not None and stored.result["hosts"] == 1  # plus enrichment


async def test_a_result_landing_after_the_cancel_leaves_the_job_cancelled(
    worker_db,
) -> None:
    job = await _seed(worker_db)

    await _race(
        worker_db,
        lambda s: JobCoreService.cancel_job(s, job.id),
        lambda s: JobCoreService.submit_job_result(s, job.id, _RESULT),
    )

    stored = await _stored(worker_db, job.id)
    assert (stored.status, stored.result) == (JobStatus.CANCELLED, None)


async def test_a_failed_result_records_its_error(worker_db) -> None:
    job = await _seed(worker_db)
    async with worker_db() as s:
        await JobCoreService.submit_job_result(
            s, job.id, JobResultSubmit(status=JobStatus.FAILED, error="ping missing")
        )
        await s.commit()

    stored = await _stored(worker_db, job.id)
    assert (stored.status, stored.error) == (JobStatus.FAILED, "ping missing")
    assert stored.completed_at is not None


async def test_a_result_refused_by_a_racing_cancel_returns_the_cancelled_job(
    worker_db,
) -> None:
    """The losing request answers with what is stored, not with what it sent."""
    job = await _seed(worker_db)

    returned = await _race(
        worker_db,
        lambda s: JobCoreService.cancel_job(s, job.id),
        lambda s: JobCoreService.submit_job_result(s, job.id, _RESULT),
    )

    assert returned is not None
    assert (returned.status, returned.result) == (JobStatus.CANCELLED, None)


async def test_the_agent_is_told_a_cancelled_job_is_cancelled(worker_db) -> None:
    job = await _seed(worker_db)

    response = await _race(
        worker_db,
        lambda s: JobCoreService.cancel_job(s, job.id),
        lambda s: JobApiCoreService.submit_job_result(
            s, job.id, JobResultSubmit(status=JobStatus.FAILED, error="boom")
        ),
    )

    assert response is not None
    assert response.status == JobStatus.CANCELLED


async def test_a_second_result_racing_the_first_returns_the_first(worker_db) -> None:
    job = await _seed(worker_db)

    returned = await _race(
        worker_db,
        lambda s: JobCoreService.submit_job_result(s, job.id, _RESULT),
        lambda s: JobCoreService.submit_job_result(
            s, job.id, JobResultSubmit(status=JobStatus.COMPLETED, result={"hosts": 9})
        ),
    )

    assert (await _stored(worker_db, job.id)).result["hosts"] == 1
    assert returned.status == JobStatus.COMPLETED
    assert returned.result["hosts"] == 1
