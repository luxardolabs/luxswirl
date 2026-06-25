"""
CRUD operations for scheduler models.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_model import JobConfiguration, JobExecution


class JobExecutionCRUD:
    """CRUD operations for JobExecution model."""

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
    async def get_by_job_key(db: AsyncSession, job_key: str, limit: int = 10) -> list[JobExecution]:
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
        result = await db.execute(delete(JobExecution).where(JobExecution.started_at < cutoff_date))
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
                JobConfiguration.enabled == True,  # noqa: E712
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
    async def get_all_ordered(db: AsyncSession) -> list[JobConfiguration]:
        """Get all jobs ordered by display_name."""
        result = await db.execute(select(JobConfiguration).order_by(JobConfiguration.display_name))
        return list(result.scalars().all())
