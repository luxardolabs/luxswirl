"""A bulk check job judges its owner when it runs, and keeps what it did.

Through the real worker (`process_next_job`, its own committing sessions):
- a job whose owner is no longer an active editor or admin changes nothing;
- a job that changed no check fails with its counts kept on it, beside the
  failure, not lost with the handler's rolled-back session;
- a failed job's `error` is a summary: no SQL statement, no parameters, no
  credential, so anyone who polls it (any API token) reads nothing secret.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import func, select, text

pytestmark = pytest.mark.integration

SECRET = "s3cret-pw"


async def _owner(maker, **overrides) -> UUID:
    user = make_user(**{"role": "editor", **overrides})
    async with maker() as s:
        s.add(user)
        await s.commit()
        return user.id


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


async def _check(maker) -> UUID:
    agent = make_agent()
    async with maker() as s:
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id)
        s.add(check)
        await s.commit()
        return check.id


async def _exists(maker, check_id: UUID) -> bool:
    async with maker() as s:
        return bool(
            await s.scalar(
                select(func.count()).select_from(Check).where(Check.id == check_id)
            )
        )


async def _run() -> None:
    register_all()
    assert await process_next_job()


@pytest.mark.parametrize(
    ("owner", "deleted"),
    [
        ({"role": "editor"}, True),
        ({"role": "viewer"}, False),
        ({"is_active": False}, False),
        (None, False),
    ],
    ids=["editor", "demoted-to-viewer", "deactivated", "no-owner"],
)
async def test_a_bulk_delete_runs_only_for_an_owner_who_may_still_edit(
    worker_db, owner: dict[str, object] | None, deleted: bool
) -> None:
    check_id = await _check(worker_db)
    owner_id = await _owner(worker_db, **owner) if owner is not None else None
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_DELETE,
        {"check_ids": [str(check_id)]},
        owner_id,
    )

    await _run()

    assert await _exists(worker_db, check_id) is not deleted
    row = await _row(worker_db, job_id)
    if not deleted:
        assert row.status == MaintenanceJobStatus.FAILED
        assert "may no longer change checks" in (row.error or "")


async def test_a_bulk_delete_of_vanished_checks_fails_with_its_counts(
    worker_db,
) -> None:
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_DELETE,
        {"check_ids": [str(uuid4())]},
        await _owner(worker_db),
    )

    await _run()

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.FAILED
    assert row.progress is not None
    assert (row.progress["success_count"], row.progress["failure_count"]) == (0, 1)


async def test_a_partly_done_bulk_delete_completes_with_its_counts(
    worker_db,
) -> None:
    check_id = await _check(worker_db)
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_DELETE,
        {"check_ids": [str(check_id), str(uuid4())]},
        await _owner(worker_db),
    )

    await _run()

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.DONE
    assert (row.progress["success_count"], row.progress["failure_count"]) == (1, 1)


async def _refuse_check_inserts(maker, on: bool) -> None:
    async with maker() as s:
        if on:
            await s.execute(
                text(
                    "CREATE OR REPLACE FUNCTION refuse_write() RETURNS trigger AS $$ "
                    "BEGIN RAISE EXCEPTION 'simulated lock failure'; END $$ "
                    "LANGUAGE plpgsql"
                )
            )
            await s.execute(
                text(
                    "CREATE TRIGGER refuse_write BEFORE INSERT ON checks "
                    "FOR EACH ROW EXECUTE FUNCTION refuse_write()"
                )
            )
        else:
            await s.execute(text("DROP TRIGGER IF EXISTS refuse_write ON checks"))
            await s.execute(text("DROP FUNCTION IF EXISTS refuse_write()"))
        await s.commit()


async def test_a_failed_job_keeps_no_sql_or_credential_in_its_error(
    worker_db,
) -> None:
    agent = make_agent()
    admin = make_user(role="admin")
    async with worker_db() as s:
        s.add_all([agent, admin])
        await s.commit()
        agent_id, admin_id = agent.id, admin.id
    job_id = await _queue(
        worker_db,
        MaintenanceJobKind.BULK_CHECK_IMPORT,
        {
            "agent_id": str(agent_id),
            "mode": "merge",
            "checks": [
                {
                    "name": "billing",
                    "check_type": "http",
                    "target": f"https://monitor:{SECRET}@93.184.216.34/health",
                }
            ],
        },
        admin_id,
    )

    await _refuse_check_inserts(worker_db, on=True)
    try:
        await _run()
    finally:
        await _refuse_check_inserts(worker_db, on=False)

    row = await _row(worker_db, job_id)
    assert row.status == MaintenanceJobStatus.FAILED
    assert row.error
    assert SECRET not in row.error
    assert "[SQL:" not in row.error and "[parameters:" not in row.error
