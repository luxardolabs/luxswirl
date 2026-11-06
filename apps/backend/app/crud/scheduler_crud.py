"""
CRUD operations for scheduler models.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import SchedulerExecutionStatus
from app.models.scheduler_model import JobConfiguration, JobExecution


class JobExecutionCRUD:
    """CRUD operations for JobExecution model."""

    @staticmethod
    async def reload(db: AsyncSession, execution: JobExecution) -> None:
        """Re-read an execution a savepoint rollback expired."""
        await db.refresh(execution)

    @staticmethod
    async def create_execution(
        db: AsyncSession,
        *,
        job_key: str,
        job_name: str,
        category: str | None = None,
        started_at: datetime | None = None,
        status: str = "running",
    ) -> JobExecution:
        """
        Create a new job execution record.

        Args:
            db: Database session
            job_key: Job identifier
            job_name: Display name of the job
            category: Job category
            started_at: When execution started (default: now)
            status: Initial status (default: running)

        Returns:
            Created JobExecution
        """
        execution = JobExecution(
            id=uuid4(),
            job_key=job_key,
            job_name=job_name,
            category=category,
            started_at=started_at or datetime.now(UTC),
            status=status,
        )
        db.add(execution)
        await db.flush()
        await db.refresh(execution)
        return execution

    @staticmethod
    async def get_by_job_key(
        db: AsyncSession, job_key: str, limit: int = 10
    ) -> list[JobExecution]:
        """Get recent executions for a job."""
        result = await db.execute(
            select(JobExecution)
            .where(JobExecution.job_key == job_key)
            .order_by(JobExecution.started_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete_before(db: AsyncSession, cutoff_date: datetime) -> int:
        """
        Delete job execution records older than cutoff date.

        Args:
            db: Database session
            cutoff_date: Delete records started before this date

        Returns:
            Number of deleted records
        """
        result = await db.execute(
            delete(JobExecution).where(JobExecution.started_at < cutoff_date)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]


class JobConfigurationCRUD:
    """CRUD operations for JobConfiguration model."""

    @staticmethod
    async def get_due_jobs(
        db: AsyncSession, now: datetime, limit: int = 10
    ) -> list[JobConfiguration]:
        """
        Get enabled jobs past next_run_at, not leased, with FOR UPDATE SKIP LOCKED.

        Uses PostgreSQL's SKIP LOCKED for distributed job queue semantics.
        """
        result = await db.execute(
            select(JobConfiguration)
            .where(
                JobConfiguration.enabled.is_(True),
                JobConfiguration.next_run_at <= now,
                JobConfiguration.lease_expires_at.is_(None)
                | (JobConfiguration.lease_expires_at <= now),
            )
            .order_by(JobConfiguration.next_run_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())

    @staticmethod
    async def add_all(db: AsyncSession, configs: list[JobConfiguration]) -> None:
        """Insert job configurations (flushed; the session owner commits)."""
        db.add_all(configs)
        await db.flush()

    @staticmethod
    async def save_job(db: AsyncSession, job: JobConfiguration) -> JobConfiguration:
        """Write the session's pending changes; the job, refreshed."""
        await db.flush()
        await db.refresh(job)
        return job

    @staticmethod
    async def count_success(
        db: AsyncSession,
        job: JobConfiguration,
        status: SchedulerExecutionStatus,
        now: datetime,
    ) -> None:
        """Count a run that finished, in one statement, and re-read the job.

        A run reads the job and then runs for as long as the job takes, so it adds to
        the row as stored, never to what it read.
        """
        await db.execute(
            update(JobConfiguration)
            .where(JobConfiguration.id == job.id)
            .values(
                total_runs=JobConfiguration.total_runs + 1,
                retry_count=0,
                last_run_at=now,
                last_status=status,
            )
            .execution_options(synchronize_session=False)
        )
        await db.refresh(job)

    @staticmethod
    async def count_failure(
        db: AsyncSession, job: JobConfiguration, *, counts_as_retry: bool
    ) -> None:
        """Count a run that failed, in one statement, and re-read the job (see
        `count_success`). A manual run is no retry. A savepoint's rollback may have
        expired the job, and reading its id must not load it, so it is re-read first."""
        await db.refresh(job)
        await db.execute(
            update(JobConfiguration)
            .where(JobConfiguration.id == job.id)
            .values(
                failed_runs=JobConfiguration.failed_runs + 1,
                retry_count=JobConfiguration.retry_count + int(counts_as_retry),
                last_status=SchedulerExecutionStatus.FAILED,
            )
            .execution_options(synchronize_session=False)
        )
        await db.refresh(job)

    @staticmethod
    async def get_by_job_key(db: AsyncSession, job_key: str) -> JobConfiguration | None:
        """Get job config by job_key string."""
        result = await db.execute(
            select(JobConfiguration).where(JobConfiguration.job_key == job_key)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_job_key_with_lease(
        db: AsyncSession, job_key: str, lease_token: UUID
    ) -> JobConfiguration | None:
        """Verify lease ownership for a job."""
        result = await db.execute(
            select(JobConfiguration).where(
                JobConfiguration.job_key == job_key,
                JobConfiguration.lease_token == lease_token,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def take_lease(
        db: AsyncSession,
        job_key: str,
        lease_token: UUID,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        """Lease the job to `lease_token` unless a live lease holds it, in one
        statement: of two callers at once, one gets it. False when it is held
        (or no job has the key)."""
        result = await db.execute(
            update(JobConfiguration)
            .where(
                JobConfiguration.job_key == job_key,
                JobConfiguration.lease_expires_at.is_(None)
                | (JobConfiguration.lease_expires_at <= now),
            )
            .values(lease_token=lease_token, lease_expires_at=expires_at)
            .returning(JobConfiguration.id)
        )
        return result.first() is not None

    @staticmethod
    async def release_lease(db: AsyncSession, job_key: str, lease_token: UUID) -> bool:
        """Clear the lease only while `lease_token` still holds it, in one statement.

        A run that outlived its lease may find the job leased again by another worker;
        clearing it by primary key would hand that worker's run to a third. False when
        the lease was no longer this run's."""
        result = await db.execute(
            update(JobConfiguration)
            .where(
                JobConfiguration.job_key == job_key,
                JobConfiguration.lease_token == lease_token,
            )
            .values(lease_token=None, lease_expires_at=None)
            .returning(JobConfiguration.id)
        )
        return result.first() is not None

    @staticmethod
    async def get_all_ordered(db: AsyncSession) -> list[JobConfiguration]:
        """Get all jobs ordered by display_name."""
        # unbounded-reads: the seeded scheduler-job catalogue, all rendered on its page
        result = await db.execute(
            select(JobConfiguration).order_by(JobConfiguration.display_name)
        )
        return list(result.scalars().all())
