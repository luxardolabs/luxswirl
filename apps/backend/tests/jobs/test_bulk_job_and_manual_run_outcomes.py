"""A job's recorded outcome must match what it did.

Found by an adversarial pass: once the bulk check operations caught their own
database errors (in a savepoint, so the session stays usable), the maintenance
handlers ignored the failure summary and the worker finalised a job that
changed nothing as DONE. And a manual scheduler run whose own success flush
failed raised out of the service instead of recording the failure.

The database errors are real: a BEFORE trigger that raises, standing in for a
lock timeout or deadlock in the cascade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.models.check_model import Check
from app.models.enum_model import (
    MaintenanceJobKind,
    MaintenanceJobStatus,
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.models.maintenance_job_model import MaintenanceJob
from app.models.scheduler_model import JobConfiguration, JobExecution
from app.services.core.scheduler_core_service import SchedulerCoreService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import select, text

pytestmark = pytest.mark.integration


async def _seed_check(maker, *, enabled: bool = True):
    agent = make_agent()
    check = make_check(agent_id=agent.id, enabled=enabled)
    async with maker() as s:
        s.add(agent)
        await s.flush()
        s.add(check)
        await s.commit()
    return check.id


async def _job(maker, kind: MaintenanceJobKind, params: dict) -> MaintenanceJob:
    """A queued job, owned by an editor (a job runs only for one who may edit)."""
    owner = make_user(role="editor")
    async with maker() as s:
        s.add(owner)
        await s.flush()
        job = MaintenanceJob(
            id=uuid4(),
            kind=kind,
            target_id=None,
            params=params,
            owner_id=owner.id,
            status=MaintenanceJobStatus.QUEUED,
        )
        s.add(job)
        await s.commit()
    return job


async def _drive(job: MaintenanceJob) -> None:
    """The worker claims the queued job, runs it and records its outcome."""
    register_all()
    assert await process_next_job()


async def _status(maker, job_id) -> MaintenanceJobStatus:
    async with maker() as s:
        return (
            await s.execute(
                select(MaintenanceJob.status).where(MaintenanceJob.id == job_id)
            )
        ).scalar_one()


async def _refuse(maker, op: str, *, on: bool = True) -> None:
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
                    f"CREATE TRIGGER refuse_write BEFORE {op} ON checks "
                    "FOR EACH ROW EXECUTE FUNCTION refuse_write()"
                )
            )
        else:
            await s.execute(text("DROP TRIGGER IF EXISTS refuse_write ON checks"))
            await s.execute(text("DROP FUNCTION IF EXISTS refuse_write()"))
        await s.commit()


class TestBulkMaintenanceJobOutcome:
    async def test_a_delete_that_works_is_done(self, worker_db):
        cid = await _seed_check(worker_db)
        job = await _job(
            worker_db, MaintenanceJobKind.BULK_CHECK_DELETE, {"check_ids": [str(cid)]}
        )

        await _drive(job)

        assert await _status(worker_db, job.id) == MaintenanceJobStatus.DONE
        async with worker_db() as s:
            assert await s.get(Check, cid) is None

    async def test_a_delete_the_database_refused_is_failed(self, worker_db):
        cid = await _seed_check(worker_db)
        job = await _job(
            worker_db, MaintenanceJobKind.BULK_CHECK_DELETE, {"check_ids": [str(cid)]}
        )
        await _refuse(worker_db, "DELETE")
        try:
            await _drive(job)
        finally:
            await _refuse(worker_db, "DELETE", on=False)

        async with worker_db() as s:
            assert await s.get(Check, cid) is not None
        assert await _status(worker_db, job.id) == MaintenanceJobStatus.FAILED

    async def test_a_toggle_the_database_refused_is_failed(self, worker_db):
        cid = await _seed_check(worker_db, enabled=True)
        job = await _job(
            worker_db,
            MaintenanceJobKind.BULK_CHECK_TOGGLE,
            {"action": "disable", "check_ids": [str(cid)]},
        )
        await _refuse(worker_db, "UPDATE")
        try:
            await _drive(job)
        finally:
            await _refuse(worker_db, "UPDATE", on=False)

        async with worker_db() as s:
            check = await s.get(Check, cid)
            assert check is not None and check.enabled is True
        assert await _status(worker_db, job.id) == MaintenanceJobStatus.FAILED

    async def test_a_partial_delete_completes_with_what_it_could(self, worker_db):
        """A check already gone does not cancel deleting the rest."""
        cid = await _seed_check(worker_db)
        job = await _job(
            worker_db,
            MaintenanceJobKind.BULK_CHECK_DELETE,
            {"check_ids": [str(cid), str(uuid4())]},
        )

        await _drive(job)

        assert await _status(worker_db, job.id) == MaintenanceJobStatus.DONE
        async with worker_db() as s:
            assert await s.get(Check, cid) is None


async def _seed_scheduled(maker, function_name: str) -> str:
    async with maker() as s:
        s.add(
            JobConfiguration(
                job_key="manual_run_job",
                function_name=function_name,
                display_name="Manual run",
                category=SchedulerJobCategory.MONITORING,
                trigger_type=SchedulerTriggerType.INTERVAL,
                interval_seconds=60,
                enabled=True,
            )
        )
        await s.commit()
    return "manual_run_job"


class TestManualRunAlwaysRecords:
    @pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
    async def test_output_that_cannot_be_stored_is_a_recorded_failure(self, worker_db):
        """The run succeeds but its output is not JSON: the success flush fails,
        and that must still come back as a recorded FAILED run, not a raise."""
        key = await _seed_scheduled(worker_db, "unstorable")
        svc = SchedulerCoreService()

        async def unstorable(**_kwargs):
            return {"ran_at": datetime.now(UTC)}

        svc.job_functions["unstorable"] = unstorable

        out = await svc.execute_job_synchronously(key)

        assert out.status == SchedulerExecutionStatus.FAILED
        assert out.job.failed_runs == 1
        async with worker_db() as s:
            (execution,) = (
                await s.execute(select(JobExecution).where(JobExecution.job_key == key))
            ).scalars()
        assert execution.status == SchedulerExecutionStatus.FAILED

    @pytest.mark.parametrize(
        "function_name",
        [
            "cleanup_old_job_executions",
            "cleanup_notification_logs",
            "cleanup_stale_agents",
            "cleanup_check_artifacts",
            "cleanup_old_sessions",
            "collect_database_metrics",
            "collect_operational_metrics",
        ],
    )
    async def test_every_registered_job_runs_by_hand(self, worker_db, function_name):
        key = await _seed_scheduled(worker_db, function_name)

        out = await SchedulerCoreService().execute_job_synchronously(key)

        assert out.status in (
            SchedulerExecutionStatus.SUCCESS,
            SchedulerExecutionStatus.WARNING,
        ), out.error
