"""Every job write survives its owner's commit (durability locks).

Locks for moving JobCoreService's session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from app.models.enum_model import JobStatus
from app.models.job_model import Job
from app.schemas.job_schema import JobCreate, JobResultSubmit
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import make_agent
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _create(maker) -> UUID:
    async with maker() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        job = await JobCoreService.create_job(
            s,
            JobCreate(
                job_type="network_scan",
                agent_id=agent.id,
                params={"subnet": "10.0.0.0/30"},
            ),
        )
        await s.commit()
        return job.id


async def _stored(maker, job_id: UUID) -> Job | None:
    async with maker() as other:
        return await other.scalar(select(Job).where(Job.id == job_id))


async def test_create_job_is_committed(worker_db) -> None:
    job_id = await _create(worker_db)
    stored = await _stored(worker_db, job_id)
    assert stored is not None and stored.status == JobStatus.PENDING


async def test_cancel_job_is_committed(worker_db) -> None:
    job_id = await _create(worker_db)
    async with worker_db() as s:
        assert await JobCoreService.cancel_job(s, job_id) is not None
        await s.commit()
    stored = await _stored(worker_db, job_id)
    assert stored is not None and stored.status == JobStatus.CANCELLED


async def test_submitted_result_is_committed(worker_db) -> None:
    job_id = await _create(worker_db)
    async with worker_db() as s:
        await JobCoreService.submit_job_result(
            s,
            job_id,
            JobResultSubmit(status=JobStatus.FAILED, error="host unreachable"),
        )
        await s.commit()
    stored = await _stored(worker_db, job_id)
    assert stored is not None and stored.status == JobStatus.FAILED
    assert stored.error == "host unreachable"


async def test_status_update_is_committed(worker_db) -> None:
    job_id = await _create(worker_db)
    async with worker_db() as s:
        await JobCoreService.update_job_status(s, job_id, JobStatus.ASSIGNED)
        await s.commit()
    stored = await _stored(worker_db, job_id)
    assert stored is not None and stored.status == JobStatus.ASSIGNED


async def test_delete_job_is_committed(worker_db) -> None:
    job_id = await _create(worker_db)
    async with worker_db() as s:
        await JobCoreService.delete_job(s, job_id)
        await s.commit()
    assert await _stored(worker_db, job_id) is None
