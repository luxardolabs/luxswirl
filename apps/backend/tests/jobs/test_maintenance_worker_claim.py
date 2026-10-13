"""The maintenance worker's claim is committed before the handler runs.

`process_next_job` claims the oldest queued job and marks it running in one
worker transaction, then hands it to the handler. Durability lock for moving the
claim's session work into crud (`fw.no_session_mutation_outside_crud`): while the
handler runs, a SEPARATE connection must already see the job RUNNING.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from app.background.maintenance_worker import HANDLERS, process_next_job
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_the_claim_is_committed_before_the_handler_runs(
    worker_db, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    seen: list[tuple[MaintenanceJobStatus, datetime | None]] = []

    async def handler(db: AsyncSession, job: MaintenanceJob) -> None:
        async with worker_db() as other:  # separate connection: only committed rows
            stored = await other.scalar(
                select(MaintenanceJob).where(MaintenanceJob.id == job.id)
            )
            assert stored is not None
            seen.append((stored.status, stored.started_at))

    monkeypatch.setitem(HANDLERS, MaintenanceJobKind.BULK_CHECK_DELETE, handler)

    assert await process_next_job() is True

    ((status, started_at),) = seen
    assert status == MaintenanceJobStatus.RUNNING
    assert started_at is not None


async def test_an_empty_queue_claims_nothing(worker_db) -> None:
    assert await process_next_job() is False
