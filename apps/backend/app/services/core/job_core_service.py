"""
Job service - business logic for job dispatch and management.

Handles:
- Creating and dispatching jobs to agents
- Fetching pending jobs for heartbeat responses
- Processing job results
- Auto-purging expired jobs
- Job queue management
"""

import asyncio
from collections.abc import Sequence
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.crud.job_crud import JobCRUD
from app.models.agent_model import Agent
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.job_schema import JobCreate, JobDispatch, JobResultSubmit
from app.services.core._job_enrichers import enrich_job_result
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.services.job")


class JobCoreService:
    """Service for job operations."""

    @staticmethod
    def resolve_runner_filter(token: str | None) -> tuple[UUID | None, bool]:
        """Resolve a runner filter token (uuid | "server" | none) into
        ``(agent_id, server_only)`` — the single interpretation of the UI token.
        """
        if not token:
            return None, False
        if token == Job.SERVER_RUNNER:
            return None, True
        return UUID(token), False

    @staticmethod
    async def create_job(
        db: AsyncSession,
        data: JobCreate,
        created_by: str | None = None,
    ) -> Job:
        """
        Create a new job.

        Args:
            db: Database session
            data: Job creation data
            created_by: User who created the job

        Returns:
            Created job

        Raises:
            AgentNotFoundException: If agent_id is specified but doesn't exist
        """
        # Verify agent exists if agent_id is specified
        if data.agent_id and not await JobCRUD.agent_exists(db, data.agent_id):
            raise AgentNotFoundException(str(data.agent_id))

        # Create job
        job = Job(
            job_type=data.job_type,
            agent_id=data.agent_id,
            params=data.params,
            priority=data.priority,
            tags=data.tags,
            created_by=created_by,
            status="pending",
            schedule=data.schedule,
            automation_config=data.automation_config,
        )

        # Set expiration based on retention policy
        retention_days = settings.server.job_retention_days
        job.set_expiration(retention_days)

        db.add(job)
        await db.flush()
        await db.refresh(job)

        logger.info(
            "Created job",
            extra={
                "job_id": str(job.id),
                "job_type": job.job_type,
                "agent_id": str(job.agent_id) if job.agent_id else None,
                "priority": job.priority,
            },
        )

        return job

    @staticmethod
    async def get_job(db: AsyncSession, job_id: UUID) -> Job | None:
        """
        Get job by ID.

        Args:
            db: Database session
            job_id: Job UUID

        Returns:
            Job instance or None if not found
        """
        return await JobCRUD.get_by_id(db, job_id)

    @staticmethod
    async def list_jobs(
        db: AsyncSession,
        agent_id: UUID | None = None,
        server_only: bool = False,
        status: str | None = None,
        job_type: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Job], int]:
        """
        List jobs with filtering and pagination.

        Args:
            db: Database session
            agent_id: Filter by agent ID
            status: Filter by status
            job_type: Filter by job type
            offset: Pagination offset
            limit: Items per page

        Returns:
            Tuple of (jobs list, total count)
        """
        return await JobCRUD.list_paginated(
            db,
            agent_id=agent_id,
            server_only=server_only,
            status=status,
            job_type=job_type,
            offset=offset,
            limit=limit,
        )

    @staticmethod
    async def get_pending_jobs_for_agent(
        db: AsyncSession,
        agent_id: UUID,
        limit: int | None = None,
    ) -> Sequence[Job]:
        """
        Get pending jobs for an agent (for heartbeat response).

        Jobs are returned in priority order (highest first).
        Only jobs in 'pending' status are returned.

        Args:
            db: Database session
            agent_id: Agent identifier
            limit: Maximum jobs to return (defaults to config value)

        Returns:
            List of pending jobs
        """
        if limit is None:
            limit = settings.server.job_max_dispatch_per_heartbeat

        jobs = await JobCRUD.get_pending_for_agent(db, agent_id, limit)

        # Mark jobs as assigned
        if jobs:
            for job in jobs:
                job.assign()

            logger.info(
                "Dispatched jobs to agent",
                extra={"job_count": len(jobs), "agent_id": str(agent_id)},
            )

        return jobs

    @staticmethod
    async def update_job_status(
        db: AsyncSession,
        job_id: UUID,
        status: JobStatus,
    ) -> Job | None:
        """
        Update job status.

        Args:
            db: Database session
            job_id: Job UUID
            status: New status

        Returns:
            Updated job or None if not found
        """
        job = await JobCoreService.get_job(db, job_id)
        if not job:
            return None

        old_status = job.status
        job.status = status

        # Update timestamps based on status
        if status == "running" and not job.started_at:
            job.start()
        elif status in ("completed", "failed", "cancelled") and not job.completed_at:
            job.completed_at = utc_now()

        await db.flush()
        await db.refresh(job)

        logger.info(
            "Job status changed",
            extra={
                "job_id": str(job_id),
                "from_status": old_status,
                "to_status": status,
            },
        )

        return job

    @staticmethod
    async def submit_job_result(
        db: AsyncSession,
        job_id: UUID,
        result_data: JobResultSubmit,
    ) -> Job | None:
        """
        Submit job results from agent.

        Args:
            db: Database session
            job_id: Job UUID
            result_data: Job result submission

        Returns:
            Updated job or None if not found
        """
        job = await JobCoreService.get_job(db, job_id)
        if not job:
            logger.warning(
                "Received results for unknown job",
                extra={"job_id": str(job_id)},
            )
            return None

        # A late/stale result must not overwrite a terminal job — e.g. an agent
        # that picked up a job before it was cancelled and reports back after.
        # Without this, a cancelled job silently flips to completed.
        if job.is_terminal:
            logger.warning(
                "Ignoring result for already-terminal job",
                extra={"job_id": str(job_id), "status": job.status},
            )
            return job

        # Enrich results using pluggable job-specific enrichment
        result_to_store = result_data.result or {}
        if result_data.status == "completed":
            result_to_store = enrich_job_result(job.job_type, result_to_store)
            logger.info(
                "Enriched results for job",
                extra={"job_id": str(job_id), "job_type": job.job_type},
            )

        # Update job with results
        if result_data.status == "completed":
            job.complete(result_to_store)
            logger.info(
                "Job completed successfully",
                extra={"job_id": str(job_id)},
            )
        else:
            job.fail(result_data.error or "Unknown error")
            logger.warning(
                "Job failed",
                extra={"job_id": str(job_id), "error_message": result_data.error},
            )

        await db.flush()
        await db.refresh(job)

        return job

    @staticmethod
    async def cancel_job(db: AsyncSession, job_id: UUID) -> Job | None:
        """
        Cancel a job.

        Only jobs in 'pending' or 'assigned' status can be cancelled.

        Args:
            db: Database session
            job_id: Job UUID

        Returns:
            Cancelled job or None if not found/cannot be cancelled
        """
        job = await JobCoreService.get_job(db, job_id)
        if not job:
            return None

        if job.status not in ("pending", "assigned"):
            logger.warning(
                "Cannot cancel job in current status",
                extra={"job_id": str(job_id), "status": job.status},
            )
            return None

        job.cancel()
        await db.flush()
        await db.refresh(job)

        logger.info("Job cancelled", extra={"job_id": str(job_id)})

        return job

    @staticmethod
    async def delete_job(db: AsyncSession, job_id: UUID) -> None:
        """
        Delete a job from the database.

        Args:
            db: Database session
            job_id: Job UUID

        Raises:
            Exception: If job not found
        """
        job = await JobCoreService.get_job(db, job_id)
        if not job:
            raise Exception(f"Job not found: {job_id}")

        await db.delete(job)

        logger.info("Job deleted", extra={"job_id": str(job_id)})

    @staticmethod
    async def purge_expired_jobs(db: AsyncSession) -> int:
        """
        Delete expired jobs that have passed their retention period.

        Only deletes jobs in terminal states (completed/failed/cancelled).

        Args:
            db: Database session

        Returns:
            Number of jobs deleted
        """
        deleted_count = await JobCRUD.delete_expired_terminal(db)

        if deleted_count > 0:
            logger.info(
                "Purged expired jobs",
                extra={"deleted_count": deleted_count},
            )

        return deleted_count

    @staticmethod
    async def get_job_stats(
        db: AsyncSession,
        agent_id: UUID | None = None,
        server_only: bool = False,
    ) -> dict:
        """
        Get job statistics.

        Args:
            db: Database session
            agent_id: Optional agent filter

        Returns:
            Dictionary with job counts by status
        """
        rows = await JobCRUD.get_status_counts(db, agent_id=agent_id, server_only=server_only)

        stats = {
            "pending": 0,
            "assigned": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }

        for status, count in rows:
            stats[str(status)] = count

        # Calculate totals
        stats["total"] = sum(stats.values())
        stats["active"] = stats["pending"] + stats["assigned"] + stats["running"]
        stats["terminal"] = stats["completed"] + stats["failed"] + stats["cancelled"]

        return stats

    @staticmethod
    async def job_to_dispatch(db: AsyncSession, job: Job) -> JobDispatch:
        """
        Convert Job model to JobDispatch schema for heartbeat response.

        Args:
            db: Database session for fetching settings
            job: Job model instance

        Returns:
            JobDispatch schema instance
        """
        # Determine timeout based on job type using configurable settings
        # Note: params["timeout"] is for ping/request timeout, NOT job execution timeout
        if job.job_type == "network_scan":
            # Network scans can take a long time for large subnets
            timeout_seconds = await SettingsCoreService.get_setting(
                db, "job_network_scan_timeout_seconds", default=600
            )
        elif job.job_type == "network_discover":
            # Discovery is quick
            timeout_seconds = await SettingsCoreService.get_setting(
                db, "job_network_discover_timeout_seconds", default=60
            )
        else:
            # Default timeout for all other job types
            timeout_seconds = await SettingsCoreService.get_setting(
                db, "job_default_timeout_seconds", default=300
            )

        return JobDispatch(
            job_id=job.id,
            job_type=job.job_type,
            params=job.params,
            priority=job.priority,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    async def get_jobs_for_dispatch(db: AsyncSession, agent_id: UUID) -> list[dict]:
        """
        Get all pending jobs for an agent in dispatch format.

        Combines get_pending_jobs_for_agent and job_to_dispatch to return
        a list of jobs ready to send in heartbeat response.

        Args:
            db: Database session
            agent_id: Agent ID string

        Returns:
            List of job dicts ready for heartbeat response
        """
        # Get pending jobs for this agent
        pending_jobs = await JobCoreService.get_pending_jobs_for_agent(db, agent_id)

        # Convert to dispatch format (async in parallel)
        jobs_dispatch = await asyncio.gather(
            *[JobCoreService.job_to_dispatch(db, job) for job in pending_jobs]
        )

        # Convert to dict format for JSON response
        return [job.model_dump() for job in jobs_dispatch]

    @staticmethod
    async def get_agents_with_jobs(db: AsyncSession) -> list[Agent]:
        """
        Get distinct agents that have jobs (excluding pending/rejected).

        Args:
            db: Database session

        Returns:
            List of Agent objects
        """
        return await JobCRUD.get_agents_with_jobs(db)

    @staticmethod
    async def get_distinct_job_types(db: AsyncSession) -> list[str]:
        """
        Get all distinct job types ordered alphabetically.

        Args:
            db: Database session

        Returns:
            The in-use job types that are valid JobType members.
        """
        valid = {t.value for t in JobType}
        return [t for t in await JobCRUD.get_distinct_job_types(db) if t in valid]

    @staticmethod
    async def list_jobs_with_agents(
        db: AsyncSession,
        status: str | None = None,
        job_type: str | None = None,
        agent_id: UUID | None = None,
        server_only: bool = False,
        priority: str | None = None,
        created: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[tuple], int]:
        """
        List jobs with agent data, filtering, and pagination.

        Args:
            db: Database session
            status: Filter by status
            job_type: Filter by job type
            agent_id: Filter by a specific agent (UUID)
            server_only: Only server-run jobs (agent_id IS NULL)
            priority: Filter by priority (high/normal/low)
            created: Filter by created time (1h/24h/7d/30d)
            limit: Max results
            offset: Pagination offset

        Returns:
            Tuple of (list of (Job, Agent|None) tuples, total count)
        """
        return await JobCRUD.list_jobs_with_agents(
            db,
            status=status,
            job_type=job_type,
            agent_id=agent_id,
            server_only=server_only,
            priority=priority,
            created=created,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    async def get_job_status_summary(db: AsyncSession) -> dict:
        """
        Get job status summary counts.

        Args:
            db: Database session

        Returns:
            Dictionary with total, per-status counts, and active count
        """
        return await JobCRUD.get_job_status_summary(db)
