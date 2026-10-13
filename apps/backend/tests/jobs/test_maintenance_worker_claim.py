"""The maintenance worker's claim hands back the job as RUNNING, and it is durable.

`_claim_one` claims the oldest queued job and marks it running in one worker
transaction. Durability lock for moving its session work into crud
(`fw.no_session_mutation_outside_crud`): the returned job must already show the
claim, and a SEPARATE connection must see it committed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.background.maintenance_worker import _claim_one
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def test_a_claimed_job_comes_back_running_and_is_committed(worker_db) -> None:
    job_id = uuid4()
    async with worker_db() as s:
        s.add(
            MaintenanceJob(
                id=job_id,
                kind=MaintenanceJobKind.BULK_CHECK_DELETE,
                target_id=None,
                params={"check_ids": []},
                status=MaintenanceJobStatus.QUEUED,
            )
        )
        await s.commit()

    claimed = await _claim_one()

    assert claimed is not None and claimed.id == job_id
    assert claimed.status == MaintenanceJobStatus.RUNNING
    assert claimed.started_at is not None
    async with worker_db() as other:  # separate connection: only committed rows
        stored = await other.scalar(
            select(MaintenanceJob.status).where(MaintenanceJob.id == job_id)
        )
    assert stored == MaintenanceJobStatus.RUNNING


async def test_an_empty_queue_claims_nothing(worker_db) -> None:
    assert await _claim_one() is None
