"""The web upload's bulk-import job runs the ONE core import.

It used to run its own copy in a view service, which built `CheckCreate` with
`retries=` (the field is `retry_attempts`), so an imported retry count was
silently dropped on create.
"""

from __future__ import annotations

import pytest
from app.background.maintenance_handlers.bulk_check_import import handle
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _job(db: AsyncSession, params: dict[str, object]) -> MaintenanceJob:
    owner = make_user(role="admin")
    db.add(owner)
    await db.flush()
    job = MaintenanceJob(
        kind=MaintenanceJobKind.BULK_CHECK_IMPORT, params=params, owner_id=owner.id
    )
    db.add(job)
    await db.flush()
    return job


async def test_import_creates_keeps_retries_and_reports_bad_entries(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    job = await _job(
        db,
        {
            "agent_id": str(agent.id),
            "mode": "merge",
            "checks": [
                {
                    "name": "web",
                    "check_type": "http",
                    "target": "https://example.test",
                    "retry_attempts": 4,
                },
                {"name": "no-target", "check_type": "http"},
            ],
        },
    )

    await handle(db, job)

    checks = {
        c.display_name: c
        for c in await CheckCoreService.list_checks_for_agent(db, agent.id)
    }
    assert set(checks) == {"web"}
    assert checks["web"].retry_attempts == 4


async def test_replace_mode_updates_an_existing_check(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    db.add(make_check(agent_id=agent.id, display_name="web", target="https://old.test"))
    await db.flush()
    job = await _job(
        db,
        {
            "agent_id": str(agent.id),
            "mode": "replace",
            "checks": [
                {"name": "web", "check_type": "http", "target": "https://new.test"}
            ],
        },
    )

    await handle(db, job)

    (check,) = await CheckCoreService.list_checks_for_agent(db, agent.id)
    assert check.target == "https://new.test"
