"""Persistence-asserting tests for SchedulerCoreService (LUXSWIRL-127 / -191).

LUXSWIRL-191: the scheduler ran jobs for ~6 weeks but COMMITTED nothing — leases,
execution records, last_run, run stats, all rolled back on session close. A
happy-path test ("the function returned a dict, no exception raised") would have
stayed green through the entire outage.

So every test here asserts CROSS-SESSION PERSISTENCE: drive the worker through
its own committing session, then open a FRESH session (via the `worker_db`
maker) and assert the write actually landed. That fresh-session read is the one
assertion the bug failed — see the `worker_db` fixture docstring.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.enum_model import (
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.models.scheduler_model import JobConfiguration, JobExecution
from app.services.core.scheduler_core_service import SchedulerCoreService

pytestmark = pytest.mark.integration


async def _seed_job(maker, **overrides) -> str:
    """Insert a COMMITTED JobConfiguration; return its job_key."""
    fields: dict = {
        "job_key": "test_job",
        "function_name": "test_noop",
        "display_name": "Test Job",
        "category": SchedulerJobCategory.MONITORING,
        "trigger_type": SchedulerTriggerType.INTERVAL,
        "interval_seconds": 60,
        "enabled": True,
    }
    fields.update(overrides)
    async with maker() as s:
        s.add(JobConfiguration(**fields))
        await s.commit()
    return fields["job_key"]


def _svc(result=None, *, raises: Exception | None = None) -> SchedulerCoreService:
    """A scheduler with one registered test function (`test_noop`)."""
    svc = SchedulerCoreService()

    async def _noop(**_kwargs):
        if raises is not None:
            raise raises
        return result if result is not None else {"ok": True}

    svc.job_functions["test_noop"] = _noop
    return svc


async def _get_job(maker, key: str) -> JobConfiguration:
    async with maker() as s:
        return (
            await s.execute(select(JobConfiguration).where(JobConfiguration.job_key == key))
        ).scalar_one()


async def _executions(maker, key: str) -> list[JobExecution]:
    async with maker() as s:
        return list(
            (await s.execute(select(JobExecution).where(JobExecution.job_key == key)))
            .scalars()
            .all()
        )


class TestScheduledRunPersists:
    """`_execute_job` — the scheduled dispatch path."""

    async def test_execute_job_commits_record_status_and_stats(self, worker_db):
        # THE LUXSWIRL-191 REGRESSION. Pre-fix, this entire block was invisible:
        # _execute_job ran the function and rolled everything back, so every
        # fresh-session read below found nothing.
        lease = uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        await _svc(result={"processed": 3})._execute_job(key, lease)

        execs = await _executions(worker_db, key)
        assert len(execs) == 1, "execution record was not committed"
        assert execs[0].status == SchedulerExecutionStatus.SUCCESS

        job = await _get_job(worker_db, key)
        assert job.last_run_at is not None, "last_run_at never advanced"
        assert job.total_runs == 1
        assert job.lease_token is None, "lease was not released"

    async def test_execute_job_with_errors_in_result_is_warning(self, worker_db):
        lease = uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        await _svc(result={"errors": ["one failed"]})._execute_job(key, lease)

        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.WARNING

    async def test_execute_job_records_failure_and_clears_lease(self, worker_db):
        lease = uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        await _svc(raises=RuntimeError("boom"))._execute_job(key, lease)

        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.FAILED

        job = await _get_job(worker_db, key)
        assert job.last_status == SchedulerExecutionStatus.FAILED
        assert job.failed_runs == 1
        assert job.lease_token is None

    async def test_execute_job_without_matching_lease_is_a_noop(self, worker_db):
        # No lease on the row → get_by_job_key_with_lease returns None → the job
        # must NOT run and must NOT create an execution record.
        key = await _seed_job(worker_db)

        await _svc()._execute_job(key, uuid4())

        assert await _executions(worker_db, key) == []


class TestProcessDueJobsPipeline:
    """`_process_due_jobs` — the poll loop: lease a due job, COMMIT the lease, then
    dispatch _execute_job. Pre-fix the lease was never committed, so the dispatched
    task re-opened its own session, found no lease, logged 'Lost lease', and bailed
    before running — the other half of LUXSWIRL-191."""

    async def test_due_job_is_leased_committed_dispatched_and_runs(self, worker_db):
        key = await _seed_job(worker_db, next_run_at=datetime.now(UTC) - timedelta(seconds=1))

        svc = _svc(result={"ok": 1})
        await svc._process_due_jobs()
        # _execute_job is dispatched as a task after the lease commits; let it run.
        await asyncio.sleep(0.3)

        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.SUCCESS
        job = await _get_job(worker_db, key)
        assert job.last_run_at is not None, "job never actually ran"
        assert job.lease_token is None, "lease not released after run"

    async def test_not_due_job_is_left_alone(self, worker_db):
        key = await _seed_job(worker_db, next_run_at=datetime.now(UTC) + timedelta(hours=1))

        await _svc()._process_due_jobs()
        await asyncio.sleep(0.1)

        assert await _executions(worker_db, key) == []


class TestManualRunPersists:
    """`execute_job_synchronously` — the admin 'Run' button path."""

    async def test_manual_run_commits_record_and_advances_last_run(self, worker_db):
        key = await _seed_job(worker_db)

        out = await _svc(result={"deleted": 5}).execute_job_synchronously(key)
        assert out["status"] == SchedulerExecutionStatus.SUCCESS

        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.SUCCESS

        job = await _get_job(worker_db, key)
        assert job.last_run_at is not None
        assert job.total_runs == 1

    async def test_failure_propagates_to_caller(self, worker_db):
        # execute_job_synchronously re-raises on failure so the admin "Run" button
        # surfaces an error toast (vs the scheduled path, which records + swallows).
        key = await _seed_job(worker_db)
        with pytest.raises(RuntimeError):
            await _svc(raises=RuntimeError("boom")).execute_job_synchronously(key)


class TestToggleAndResetPersist:
    """`toggle_job` / `reset_job` — also session-owning writes that never committed."""

    async def test_toggle_persists(self, worker_db):
        key = await _seed_job(worker_db, enabled=True)

        await SchedulerCoreService().toggle_job(key)

        assert (await _get_job(worker_db, key)).enabled is False

    async def test_reset_clears_failures_and_reenables(self, worker_db):
        key = await _seed_job(worker_db, enabled=False, retry_count=3, failed_runs=2)

        await SchedulerCoreService().reset_job(key)

        job = await _get_job(worker_db, key)
        assert job.enabled is True
        assert job.retry_count == 0
        assert job.failed_runs == 0


class TestRetryAndAutoDisable:
    """`_handle_job_failure` decides whether a flapping job keeps retrying or gets
    auto-disabled. Wrong threshold = either a broken job runs forever, or one
    transient blip disables a critical job. Driven through the real _execute_job
    failure path so the retry/disable decision is persistence-asserted."""

    async def _run_one_failure(self, worker_db, **job_overrides) -> JobConfiguration:
        lease = uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            **job_overrides,
        )
        await _svc(raises=RuntimeError("boom"))._execute_job(key, lease)
        return await _get_job(worker_db, key)

    async def test_disables_once_retry_count_exceeds_limit(self, worker_db):
        # retry_count 2 -> 3, limit 2 -> 3 > 2 -> disabled
        job = await self._run_one_failure(
            worker_db, retry_limit=2, retry_count=2, backoff_seconds=10
        )
        assert job.enabled is False
        assert job.retry_count == 3
        assert job.failed_runs == 1

    async def test_below_limit_reschedules_and_stays_enabled(self, worker_db):
        job = await self._run_one_failure(
            worker_db, retry_limit=5, retry_count=0, backoff_seconds=10
        )
        assert job.enabled is True
        assert job.retry_count == 1
        assert job.next_run_at is not None
        assert job.next_run_at > datetime.now(UTC)

    async def test_retry_limit_zero_never_disables_and_caps_backoff(self, worker_db):
        # retry_count 50 -> backoff would overflow; retry_limit=0 caps at 3600s and
        # never disables (critical-job semantics).
        now = datetime.now(UTC)
        job = await self._run_one_failure(
            worker_db, retry_limit=0, retry_count=50, backoff_seconds=10
        )
        assert job.enabled is True
        assert job.next_run_at is not None
        assert job.next_run_at <= now + timedelta(seconds=3610)


class TestTimeoutHandling:
    """A job that blows its max_runtime must be recorded FAILED (scheduled) or
    surfaced (manual) — otherwise a hung job looks healthy forever."""

    async def test_scheduled_timeout_is_recorded_failed(self, worker_db):
        lease = uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        await _svc(raises=TimeoutError())._execute_job(key, lease)

        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.FAILED

    async def test_manual_timeout_propagates(self, worker_db):
        key = await _seed_job(worker_db)
        with pytest.raises(TimeoutError):
            await _svc(raises=TimeoutError()).execute_job_synchronously(key)


class TestJobHistory:
    async def test_returns_job_and_its_executions(self, worker_db):
        lease = uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        await _svc(result={"ok": 1})._execute_job(key, lease)

        job, executions = await SchedulerCoreService().get_job_history(key)
        assert job.job_key == key
        assert len(executions) == 1
