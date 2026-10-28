"""Migration 011 encrypts every maintenance job's params already stored, and
downgrading writes them back as JSON.

The suite builds its schema with alembic, so this runs the revision itself, down
and up again, around a job stored as the JSON column stored it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.maintenance_job_model import MaintenanceJob
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "011_encrypt_maintenance_job_params.py"
)

SECRET = "postgresql://m:hunter2@db.example.com/app"


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev011", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def _stored(db: AsyncSession, job_id: Any) -> str:
    return await db.scalar(
        text("SELECT params::text FROM maintenance_jobs WHERE id = :id"),
        {"id": job_id},
    )


async def test_existing_params_are_encrypted_and_still_read(
    db: AsyncSession, field_key: str
) -> None:
    key = field_key
    params = {"agent_id": str(uuid4()), "checks": [{"connection_string": SECRET}]}

    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    job_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO maintenance_jobs (id, kind, params, status, created_at)"
            " VALUES (:id, 'bulk_check_import', CAST(:p AS json), 'queued', now())"
        ),
        {"id": job_id, "p": json.dumps(params)},
    )
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))

    stored = await _stored(db, job_id)
    assert "hunter2" not in stored, stored
    assert json.loads(Fernet(key.encode()).decrypt(stored.encode())) == params
    db.expire_all()
    job = await db.scalar(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
    assert job is not None and job.params == params

    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    assert json.loads(await _stored(db, job_id)) == params
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))
