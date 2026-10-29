"""Persistence-asserting tests for SchedulerCoreService.

A scheduler that runs jobs but COMMITS nothing — leases, execution records,
last_run, run stats, all rolled back on session close — passes a happy-path test
("the function returned a dict, no exception raised") the whole time.

So every test here asserts CROSS-SESSION PERSISTENCE: drive the worker through
its own committing session, then open a FRESH session (via the `worker_db`
maker) and assert the write actually landed. That fresh-session read is the one
assertion the bug failed — see the `worker_db` fixture docstring.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.crud.scheduler_crud import JobConfigurationCRUD
from app.models.enum_model import (
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)
from app.models.scheduler_model import JobConfiguration, JobExecution
from app.services.core.scheduler_core_service import SchedulerCoreService
from sqlalchemy import select

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


def _overrunning_svc() -> SchedulerCoreService:
    """A scheduler whose test function runs past any limit under a minute."""
    svc = SchedulerCoreService()

    async def _slow(**_kwargs):
        await asyncio.sleep(60)
        return {"ok": True}

    svc.job_functions["test_noop"] = _slow
    return svc


async def _get_job(maker, key: str) -> JobConfiguration:
    async with maker() as s:
        return (
            await s.execute(
                select(JobConfiguration).where(JobConfiguration.job_key == key)
            )
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
        # If _execute_job ran the function and rolled everything back, every
        # fresh-session read below would find nothing.
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

    async def test_a_lease_taken_over_during_the_run_is_left_to_its_new_holder(
        self, worker_db
    ):
        # A run that outlives its lease: another worker leases the job while this
        # one is still running. Releasing by primary key cleared the NEW holder's
        # lease; the release must only match while this run still holds it.
        lease, takeover = uuid4(), uuid4()
        key = await _seed_job(
            worker_db,
            lease_token=lease,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        svc = SchedulerCoreService()

        async def _overrun(**_kwargs):
            async with worker_db() as s:
                job = (
                    await s.execute(
                        select(JobConfiguration).where(JobConfiguration.job_key == key)
                    )
                ).scalar_one()
                job.lease_token = takeover
                await s.commit()
            return {"ok": True}

        svc.job_functions["test_noop"] = _overrun
        await svc._execute_job(key, lease)

        job = await _get_job(worker_db, key)
        assert job.lease_token == takeover, "the new holder's lease was cleared"

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
    dispatch _execute_job. An uncommitted lease leaves the dispatched task to
    re-open its own session, find no lease, log 'Lost lease' and bail before
    running."""

    async def test_due_job_is_leased_committed_dispatched_and_runs(self, worker_db):
        key = await _seed_job(
            worker_db, next_run_at=datetime.now(UTC) - timedelta(seconds=1)
        )

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
        key = await _seed_job(
            worker_db, next_run_at=datetime.now(UTC) + timedelta(hours=1)
        )

        await _svc()._process_due_jobs()
        await asyncio.sleep(0.1)

        assert await _executions(worker_db, key) == []


class TestManualRunPersists:
    """`execute_job_synchronously` — the admin 'Run' button path."""

    async def test_manual_run_commits_record_and_advances_last_run(self, worker_db):
        key = await _seed_job(worker_db)

        out = await _svc(result={"deleted": 5}).execute_job_synchronously(key)
        assert out.status == SchedulerExecutionStatus.SUCCESS
        assert out.job.total_runs == 1

        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.SUCCESS

        job = await _get_job(worker_db, key)
        assert job.last_run_at is not None
        assert job.total_runs == 1

    async def test_failure_is_returned_and_recorded(self, worker_db):
        """A failed manual run used to re-raise inside the worker session, which
        rolled back the FAILED record it had just written — and the Run button
        answered a 500 rather than an error toast."""
        key = await _seed_job(worker_db)

        out = await _svc(raises=RuntimeError("boom")).execute_job_synchronously(key)

        assert out.status == SchedulerExecutionStatus.FAILED
        assert out.error == "boom"
        assert out.toast == (
            f"Job failed ({out.duration_seconds:.1f}s) - boom",
            "error",
        )
        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.FAILED
        assert execution.error_message == "boom"
        assert (await _get_job(worker_db, key)).failed_runs == 1
        assert out.job.failed_runs == 1

    async def test_a_failed_manual_run_is_logged_once_with_its_traceback(
        self, worker_db, caplog
    ):
        key = await _seed_job(worker_db)

        with caplog.at_level(logging.WARNING):
            await _svc(raises=RuntimeError("boom")).execute_job_synchronously(key)

        logged = [
            r for r in caplog.records if r.getMessage() == "Manual job run failed"
        ]
        assert len(logged) == 1, caplog.text
        assert logged[0].exc_info and logged[0].exc_info[0] is RuntimeError

    async def test_a_timed_out_manual_run_is_logged_once_without_a_traceback(
        self, worker_db, caplog
    ):
        key = await _seed_job(worker_db, max_runtime_seconds=1)

        with caplog.at_level(logging.WARNING):
            out = await _overrunning_svc().execute_job_synchronously(key)

        assert out.error == "Job timed out after 1 seconds"

        logged = [
            r for r in caplog.records if r.getMessage() == "Manual job run failed"
        ]
        assert len(logged) == 1, caplog.text
        assert not logged[0].exc_info

    async def test_a_timeout_after_the_job_row_changed_is_still_recorded(
        self, worker_db, monkeypatch
    ):
        """The savepoint's rollback expires the job row the success bookkeeping
        changed; the timeout's message must not read it back."""
        key = await _seed_job(worker_db)
        real_save = JobConfigurationCRUD.save_job
        calls = {"n": 0}

        async def times_out_once(db, job):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError()
            return await real_save(db, job)

        monkeypatch.setattr(
            JobConfigurationCRUD, "save_job", staticmethod(times_out_once)
        )

        out = await _svc().execute_job_synchronously(key)

        assert out.status == SchedulerExecutionStatus.FAILED
        assert out.error == "TimeoutError"
        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.FAILED

    async def test_a_job_raising_its_own_timeout_is_a_failure_with_its_cause(
        self, worker_db, caplog
    ):
        """A connect or query timeout inside the job is not the run's limit."""
        key = await _seed_job(worker_db)

        with caplog.at_level(logging.WARNING):
            out = await _svc(
                raises=TimeoutError("connect timed out")
            ).execute_job_synchronously(key)

        assert out.status == SchedulerExecutionStatus.FAILED
        assert out.error == "connect timed out"
        logged = [
            r for r in caplog.records if r.getMessage() == "Manual job run failed"
        ]
        assert len(logged) == 1, caplog.text
        assert logged[0].exc_info and logged[0].exc_info[0] is TimeoutError

    async def test_an_unregistered_function_is_a_recorded_failure(self, worker_db):
        key = await _seed_job(worker_db, function_name="no_such_function")

        out = await _svc().execute_job_synchronously(key)

        assert out.status == SchedulerExecutionStatus.FAILED
        assert "no_such_function" in (out.error or "")
        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.FAILED


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

    async def test_manual_timeout_is_recorded_failed(self, worker_db):
        key = await _seed_job(worker_db, max_runtime_seconds=1)

        out = await _overrunning_svc().execute_job_synchronously(key)

        assert out.status == SchedulerExecutionStatus.FAILED
        assert "timed out" in (out.error or "")
        (execution,) = await _executions(worker_db, key)
        assert execution.status == SchedulerExecutionStatus.FAILED


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
