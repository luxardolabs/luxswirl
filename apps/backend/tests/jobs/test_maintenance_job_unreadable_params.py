"""A queued maintenance job whose params the configured key cannot read stops the
queue, loudly, with nothing lost: every job runs once the right key is set. Its
status still reads, since nothing that reports a job needs its params."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.core.config import settings
from app.core.exceptions import DatabaseException
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from cryptography.fernet import Fernet
from fixtures.factories import make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration


def _job(owner_id: Any, params: dict[str, int]) -> MaintenanceJob:
    return MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.STATUS_PAGE_DELETE,
        target_id=uuid4(),
        owner_id=owner_id,
        status=MaintenanceJobStatus.QUEUED,
        params=params,
    )


async def _statuses(worker_db: Any, ids: tuple[Any, Any]) -> list[Any]:
    async with worker_db() as s:
        rows = dict(
            (
                await s.execute(
                    select(MaintenanceJob.id, MaintenanceJob.status).where(
                        MaintenanceJob.id.in_(ids)
                    )
                )
            ).all()
        )
    return [rows[job_id] for job_id in ids]


async def _polled_status(worker_db: Any, job_id: Any) -> Any:
    """The job as a status poll reads it, under the key configured now."""
    async with worker_db() as s:
        return (await MaintenanceJobCoreService.get_by_id(s, job_id)).status


async def test_a_wrong_key_stops_the_queue_and_the_right_key_runs_every_job(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    register_all()
    right_key = settings.security.field_encryption_key
    async with worker_db() as s:
        user = make_user(role="editor")
        s.add(user)
        await s.flush()
        first, second = _job(user.id, {"a": 1}), _job(user.id, {"b": 2})
        s.add(first)
        await s.commit()
        s.add(second)
        await s.commit()
        ids = (first.id, second.id)

    monkeypatch.setattr(
        settings.security, "field_encryption_key", Fernet.generate_key().decode()
    )
    for _ in range(2):
        with pytest.raises(DatabaseException):
            await process_next_job()
    stopped = await _statuses(worker_db, ids)
    polled = await _polled_status(worker_db, ids[0])

    monkeypatch.setattr(settings.security, "field_encryption_key", right_key)
    worked = [await process_next_job(), await process_next_job()]
    resumed = await _statuses(worker_db, ids)

    assert stopped == [MaintenanceJobStatus.QUEUED] * 2, stopped
    assert polled == MaintenanceJobStatus.QUEUED, polled
    assert "Maintenance queue stopped" in caplog.text
    assert worked == [True, True], worked
    assert MaintenanceJobStatus.QUEUED not in resumed, resumed
