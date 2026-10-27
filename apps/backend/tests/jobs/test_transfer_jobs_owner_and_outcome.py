"""Bulk create and import jobs judge their owner when they run, and keep what they did.

Through the real worker (`process_next_job`, its own committing sessions):
- a bulk create runs only for an owner who may still edit, an import only for
  one who is still an active admin; otherwise nothing changes;
- an import onto an agent deleted since it was queued fails and makes nothing;
- each keeps its counts and first errors on the job, readable by anyone who
  polls it, so no credential reaches them.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

SECRET = "s3cret-pw"


async def _owner(maker, **fields: object) -> UUID:
    user = make_user(**fields)
    async with maker() as s:
        s.add(user)
        await s.commit()
        return user.id


async def _agent(maker) -> UUID:
    agent = make_agent()
    async with maker() as s:
        s.add(agent)
        await s.commit()
        return agent.id


async def _queue(maker, kind, params, owner_id: UUID | None) -> UUID:
    job = MaintenanceJob(
        id=uuid4(),
        kind=kind,
        target_id=None,
        params=params,
        owner_id=owner_id,
        status=MaintenanceJobStatus.QUEUED,
    )
    async with maker() as s:
        s.add(job)
        await s.commit()
    return job.id


async def _row(maker, job_id: UUID) -> MaintenanceJob:
    async with maker() as s:
        return (
            await s.execute(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
        ).scalar_one()


async def _checks(maker, agent_id: UUID) -> dict[str, Check]:
    async with maker() as s:
        rows = await s.scalars(select(Check).where(Check.agent_id == agent_id))
        return {c.display_name: c for c in rows}


async def _run() -> None:
    register_all()
    assert await process_next_job()


# ── Who the job runs for ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("owner", "made"),
    [
        ({"role": "editor"}, True),
        ({"role": "admin"}, True),
        ({"role": "viewer"}, False),
        ({"role": "editor", "is_active": False}, False),
        (None, False),
    ],
    ids=["editor", "admin", "demoted-to-viewer", "deactivated", "no-owner"],
)
async def test_a_bulk_create_runs_only_for_an_owner_who_may_still_edit(
    worker_db, owner: dict[str, object] | None, made: bool
) -> None:
    agent_id = await _agent(worker_db)
    owner_id = await _owner(worker_db, **owner) if owner is not None else None
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {"agent_id": str(agent_id), "requests": [{"url": "https://one.example.test"}]},
        owner_id,
    )

    await _run()

    assert bool(await _checks(worker_db, agent_id)) is made
    row = await _row(worker_db, job_id)
    if not made:
        assert row.status == MaintenanceJobStatus.FAILED
        assert "no longer an active editor or admin" in (row.error or "")


@pytest.mark.parametrize(
    ("owner", "made"),
    [
        ({"role": "admin"}, True),
        ({"role": "editor"}, False),
        ({"role": "admin", "is_active": False}, False),
        (None, False),
    ],
    ids=["admin", "demoted-to-editor", "deactivated", "no-owner"],
)
async def test_an_import_runs_only_for_an_owner_who_is_still_an_admin(
    worker_db, owner: dict[str, object] | None, made: bool
) -> None:
    agent_id = await _agent(worker_db)
    owner_id = await _owner(worker_db, **owner) if owner is not None else None
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {"name": "web", "check_type": "http", "target": "https://x.test"}
            ],
        },
        owner_id,
    )

    await _run()

    assert bool(await _checks(worker_db, agent_id)) is made
    row = await _row(worker_db, job_id)
    if not made:
        assert row.status == MaintenanceJobStatus.FAILED
        assert "no longer an active admin" in (row.error or "")


async def test_an_import_onto_a_deleted_agent_makes_nothing(worker_db) -> None:
    gone = uuid4()
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(gone),
            "mode": "merge",
            "checks": [
                {"name": "web", "check_type": "http", "target": "https://x.test"}
            ],
        },
        await _owner(worker_db, role="admin"),
    )

    await _run()

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.FAILED
    assert row.error == "The agent no longer exists; nothing was imported"
    async with worker_db() as s:
        assert not await s.scalar(
            select(func.count()).select_from(Agent).where(Agent.id == gone)
        )


# ── What the job keeps ──────────────────────────────────────────────────────


async def test_an_import_keeps_its_counts_and_masked_errors(worker_db) -> None:
    agent_id = await _agent(worker_db)
    async with worker_db() as s:
        s.add(make_check(agent_id=agent_id, display_name="kept"))
        await s.commit()
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {"name": "new", "check_type": "http", "target": "https://x.test"},
                {"name": "kept", "check_type": "http", "target": "https://y.test"},
                {
                    "name": "leaky",
                    "check_type": "mysql",
                    "target": f"mysql://monitor:{SECRET}@db.test/app",
                    "interval": "every minute",
                },
                "not a check",
            ],
        },
        await _owner(worker_db, role="admin"),
    )

    await _run()

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.DONE, row.error
    progress = row.progress or {}
    assert (progress["created"], progress["updated"], progress["skipped"]) == (1, 0, 1)
    assert progress["failure_count"] == 2
    assert progress["errors"][0].startswith("leaky: ")
    assert SECRET not in json.dumps(progress)


async def test_a_bulk_create_keeps_its_counts_and_masked_errors(worker_db) -> None:
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {
            "agent_id": str(agent_id),
            "requests": [
                {"url": "https://ok.example.test"},
                {"url": f"synthetic://monitor:{SECRET}@x.test"},
            ],
        },
        await _owner(worker_db, role="editor"),
    )

    await _run()

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.DONE, row.error
    progress = row.progress or {}
    assert (progress["success_count"], progress["failure_count"]) == (1, 1)
    assert SECRET not in json.dumps(progress)


async def test_a_bulk_create_that_makes_nothing_fails_with_its_counts(
    worker_db,
) -> None:
    agent_id = await _agent(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        {
            "agent_id": str(agent_id),
            "requests": [{"url": f"synthetic://monitor:{SECRET}@x.test"}],
        },
        await _owner(worker_db, role="editor"),
    )

    await _run()

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.FAILED
    assert (row.progress or {})["failure_count"] == 1
    assert SECRET not in (row.error or "") + json.dumps(row.progress)
