"""A start notice racing the job's result never reopens the finished job.

The agent's start notice and its result (or an operator's cancel) can reach the
server at the same moment. The move to running is one statement that only
matches an unfinished job, so whichever lands second sees the first's
committed state: a job completed meanwhile stays completed.
"""

from __future__ import annotations

import asyncio

import pytest
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.job_schema import JobResultSubmit
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import make_agent
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _seed(maker) -> Job:
    async with maker() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        job = Job(
            job_type=JobType.NETWORK_SCAN,
            agent_id=agent.id,
            params={"subnet": "10.0.0.0/24"},
            status=JobStatus.ASSIGNED,
        )
        s.add(job)
        await s.commit()
        return job


async def _stored(maker, job_id) -> Job:
    async with maker() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def test_a_start_landing_after_the_result_leaves_the_job_completed(
    worker_db,
) -> None:
    job = await _seed(worker_db)
    async with worker_db() as s1, worker_db() as s2:
        await JobCoreService.submit_job_result(
            s1, job.id, JobResultSubmit(status=JobStatus.COMPLETED, result={"hosts": 1})
        )
        start = asyncio.create_task(
            JobCoreService.update_job_status(s2, job.id, JobStatus.RUNNING)
        )
        await asyncio.sleep(0.3)
        await s1.commit()
        await start
        await s2.commit()

    stored = await _stored(worker_db, job.id)
    assert stored.status == JobStatus.COMPLETED
    assert stored.started_at is None


async def test_a_start_alone_makes_the_job_running(worker_db) -> None:
    job = await _seed(worker_db)
    async with worker_db() as s:
        await JobCoreService.update_job_status(s, job.id, JobStatus.RUNNING)
        await s.commit()

    stored = await _stored(worker_db, job.id)
    assert stored.status == JobStatus.RUNNING
    assert stored.started_at is not None
