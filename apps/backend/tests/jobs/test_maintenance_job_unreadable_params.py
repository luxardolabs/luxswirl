"""A queued maintenance job whose params the configured key cannot read fails,
and the queue goes on: left queued, it would be claimed again on every poll and
hold back every job behind it."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.core.config import settings
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from cryptography.fernet import Fernet
from fixtures.factories import make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def test_a_job_under_another_key_fails_and_the_next_one_runs(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_all()
    configured = settings.security.field_encryption_key
    async with worker_db() as s:
        user = make_user(role="editor")
        s.add(user)
        await s.flush()
        monkeypatch.setattr(
            settings.security, "field_encryption_key", Fernet.generate_key().decode()
        )
        unreadable = MaintenanceJob(
            id=uuid4(),
            kind=MaintenanceJobKind.STATUS_PAGE_DELETE,
            target_id=uuid4(),
            owner_id=user.id,
            status=MaintenanceJobStatus.QUEUED,
            params={"a": 1},
        )
        s.add(unreadable)
        await s.commit()
        monkeypatch.setattr(settings.security, "field_encryption_key", configured)
        readable = MaintenanceJob(
            id=uuid4(),
            kind=MaintenanceJobKind.STATUS_PAGE_DELETE,
            target_id=uuid4(),
            owner_id=user.id,
            status=MaintenanceJobStatus.QUEUED,
            params={"b": 2},
        )
        s.add(readable)
        await s.commit()
        ids = (unreadable.id, readable.id)

    worked = [await process_next_job(), await process_next_job()]

    async with worker_db() as s:
        rows = {
            row.id: row
            for row in (
                await s.execute(
                    select(
                        MaintenanceJob.id, MaintenanceJob.status, MaintenanceJob.error
                    ).where(MaintenanceJob.id.in_(ids))
                )
            ).all()
        }
    assert worked == [True, True], worked
    assert rows[ids[0]].status == MaintenanceJobStatus.FAILED
    assert "do not decrypt" in (rows[ids[0]].error or "")
    assert rows[ids[1]].status != MaintenanceJobStatus.QUEUED
