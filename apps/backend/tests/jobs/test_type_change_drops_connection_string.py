"""A database check turned into a web check by a replace import keeps its
connection string, which every reader of `Check.connection_string` now serves."""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration

SECRET = "postgresql://m:hunter2@db.example.com/app"


async def _setup(maker):
    agent = make_agent()
    user = make_user(role="admin")
    async with maker() as s:
        s.add_all([agent, user])
        await s.flush()
        s.add(
            make_check(
                agent_id=agent.id,
                display_name="x",
                check_type="postgres",
                target="db.example.com",
                check_config={"connection_string": SECRET, "query": "SELECT 1"},
            )
        )
        await s.commit()
    return agent.id, user.id


async def _replace(maker, agent_id, owner_id, entry):
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
        params={"agent_id": str(agent_id), "mode": "replace", "checks": [entry]},
        owner_id=owner_id,
        status=MaintenanceJobStatus.QUEUED,
    )
    async with maker() as s:
        s.add(job)
        await s.commit()
    register_all()
    assert await process_next_job()


async def test_control_the_type_change_applies(worker_db) -> None:
    agent_id, owner = await _setup(worker_db)
    await _replace(
        worker_db,
        agent_id,
        owner,
        {"name": "x", "check_type": "http", "target": "https://a.example.com"},
    )
    async with worker_db() as s:
        check = (await s.scalars(select(Check).where(Check.agent_id == agent_id))).one()
    assert check.check_type == "http"


async def test_a_web_check_carries_no_connection_string(worker_db) -> None:
    agent_id, owner = await _setup(worker_db)
    await _replace(
        worker_db,
        agent_id,
        owner,
        {"name": "x", "check_type": "http", "target": "https://a.example.com"},
    )
    async with worker_db() as s:
        doc = await CheckTransferCoreService.export_document(s, agent_id)
    (entry,) = doc.checks
    assert entry.check_type == "http"
    assert entry.connection_string is None, entry.connection_string
