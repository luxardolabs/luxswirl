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
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AgentNotFoundException,
    JobNotFoundException,
    ValidationException,
)
from app.crud.job_crud import JobCRUD
from app.models.enum_model import (
    SERVER_RUNNER,
    UNADMITTED_AGENT_STATUSES,
    JobStatus,
    JobType,
)
from app.models.job_model import Job
from app.schemas.agent_schema import AgentOption
from app.schemas.job_schema import (
    JobCreate,
    JobDetail,
    JobDispatch,
    JobListRow,
    JobResultSubmit,
)
from app.services.core._job_enrichers import enrich_job_result
from app.services.core.network_scan_core_service import NetworkScanCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.jobs.base import BaseJob
from app.shared.jobs.network_discover import NetworkDiscoverJob
from app.shared.jobs.network_scan import NetworkScanJob
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.job")

# The job class behind each job type: its params schema, runner and labels.
JOB_CLASSES: dict[JobType, type[BaseJob]] = {
    JobType.NETWORK_SCAN: NetworkScanJob,
    JobType.NETWORK_DISCOVER: NetworkDiscoverJob,
}


class JobCoreService:
    """Service for job operations."""

    @staticmethod
    def resolve_runner_filter(token: str | None) -> tuple[UUID | None, bool]:
        """Resolve a runner filter token (uuid | "server" | none) into
        ``(agent_id, server_only)`` — the single interpretation of the UI token.
        """
        if not token:
            return None, False
        if token == SERVER_RUNNER:
            return None, True
        return UUID(token), False

    @staticmethod
    def validated_params(job_type: JobType, params: dict[str, Any]) -> dict[str, Any]:
        """`params` held to the job type's schema, with its defaults filled in.

        A key the schema does not name is dropped.

        Raises:
            ValidationException: If the params do not meet the schema
        """
        try:
            parsed = JOB_CLASSES[job_type].params_schema.model_validate(params)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in err['loc']) or 'params'}: {err['msg']}"
                for err in e.errors(include_input=False)
            )
            raise ValidationException(f"Invalid {job_type} params: {problems}") from e
        return parsed.model_dump(mode="json")

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
            ValidationException: If the job type needs an agent and none is
                named, or its params are refused (a scan subnet in the cloud
                metadata range, among others)
        """
        # Refused here, at once, rather than by the agent when it runs the job.
        if JOB_CLASSES[data.job_type].requires_agent and data.agent_id is None:
            raise ValidationException(
                f"A {data.job_type} job runs on an agent: name its agent_id"
            )
        params = JobCoreService.validated_params(data.job_type, data.params)

        if data.agent_id:
            approval = await JobCRUD.agent_approval_status(db, data.agent_id)
            if approval is None:
                raise AgentNotFoundException(str(data.agent_id))
            if approval in UNADMITTED_AGENT_STATUSES:
                raise ValidationException(
                    f"Agent {data.agent_id} is {approval}: it takes no jobs"
                )

        # Create job
        job = Job(
            job_type=data.job_type,
            agent_id=data.agent_id,
            params=params,
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

        job = await JobCRUD.add_job(db, job)

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
    async def get_job_detail(db: AsyncSession, job_id: UUID) -> JobDetail | None:
        """The job as the detail panel needs it — enriched, and a DTO.

        A network_scan result is enriched here rather than in the view: the
        view used to write the enriched payload back onto the ORM row, which
        both put business logic above the seam and risked flushing a display
        transform into the database.
        """
        job = await JobCoreService.get_job(db, job_id)
        if job is None:
            return None

        detail = JobDetail.model_validate(job)
        if detail.job_type == JobType.NETWORK_SCAN and detail.result:
            detail.result = NetworkScanCoreService.enrich_result(detail.result)
        return detail

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
            skip=offset,
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
        bound = (
            limit
            if limit is not None
            else settings.server.job_max_dispatch_per_heartbeat
        )
        jobs = await JobCRUD.get_pending_for_agent(db, agent_id, bound)

        # Mark jobs as assigned
        if jobs:
            for job in jobs:
                job.assign()

            logger.info(
                "Dispatched jobs to agent",
                # post-db-filter: log field: every job listed was just assigned
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

        A finished job (completed, failed, cancelled) keeps its state: a late
        notice from an agent does not reopen it. Only an assigned job starts
        running. Setting the status a job already has changes nothing, so a
        repeated start keeps its first `started_at`.

        Args:
            db: Database session
            job_id: Job UUID
            status: New status

        Returns:
            The job (updated, or as it was) or None if not found
        """
        moved = await JobCRUD.move_open_job(db, job_id, JobStatus(status))
        if moved is not None:
            logger.info(
                "Job status changed",
                extra={"job_id": str(job_id), "to_status": status},
            )
            return moved

        job = await JobCRUD.get_current(db, job_id)
        if job is not None and job.is_terminal:
            logger.warning(
                "Ignoring status change for already-terminal job",
                extra={"job_id": str(job_id), "status": job.status},
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

        # A late/stale result must not overwrite a finished job, such as one
        # cancelled after its agent picked it up: the move below only matches an
        # unfinished job, so a cancel landing at the same moment also holds.
        if result_data.status == JobStatus.COMPLETED:
            enriched = enrich_job_result(job.job_type, result_data.result or {})
            moved = await JobCRUD.move_open_job(
                db, job_id, JobStatus.COMPLETED, result=enriched
            )
        else:
            moved = await JobCRUD.move_open_job(
                db, job_id, JobStatus.FAILED, error=result_data.error or "Unknown error"
            )
        if moved is None:
            # As stored now: another request finished the job first.
            current = await JobCRUD.get_current(db, job_id)
            logger.warning(
                "Ignoring result for already-terminal job",
                extra={
                    "job_id": str(job_id),
                    "status": current.status if current else None,
                },
            )
            return current

        if moved.status == JobStatus.COMPLETED:
            logger.info("Job completed successfully", extra={"job_id": str(job_id)})
        else:
            logger.warning(
                "Job failed",
                extra={"job_id": str(job_id), "error_message": result_data.error},
            )
        return moved

    @staticmethod
    async def cancel_job(db: AsyncSession, job_id: UUID) -> Job | None:
        """
        Cancel a job.

        Jobs in 'pending', 'assigned' or 'running' status can be cancelled.

        Args:
            db: Database session
            job_id: Job UUID

        Returns:
            Cancelled job or None if not found/cannot be cancelled
        """
        # One statement that only matches an unfinished job, so a result
        # landing at the same moment is never overwritten.
        cancelled = await JobCRUD.move_open_job(db, job_id, JobStatus.CANCELLED)
        if cancelled is None:
            logger.warning(
                "Cannot cancel job in current status",
                extra={"job_id": str(job_id)},
            )
            return None

        logger.info("Job cancelled", extra={"job_id": str(job_id)})
        return cancelled

    @staticmethod
    async def delete_job(db: AsyncSession, job_id: UUID) -> None:
        """
        Delete a job from the database.

        Args:
            db: Database session
            job_id: Job UUID

        Raises:
            JobNotFoundException: If there is no such job
        """
        job = await JobCoreService.get_job(db, job_id)
        if not job:
            raise JobNotFoundException(job_id)

        await JobCRUD.delete_job(db, job)

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
    async def get_jobs_for_dispatch(
        db: AsyncSession, agent_id: UUID
    ) -> list[dict[str, Any]]:
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
    async def get_agents_with_jobs(db: AsyncSession) -> list[AgentOption]:
        """
        Get distinct agents that have jobs (excluding pending/rejected).

        Args:
            db: Database session

        Returns:
            The agents, as picker options
        """
        return [
            AgentOption.model_validate(agent)
            for agent in await JobCRUD.get_agents_with_jobs(db)
        ]

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
    ) -> tuple[list[JobListRow], int]:
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
            Tuple of (job rows, total count)
        """
        rows, total = await JobCRUD.list_jobs_with_agents(
            db,
            status=status,
            job_type=job_type,
            agent_id=agent_id,
            server_only=server_only,
            priority=priority,
            created=created,
            skip=offset,
            limit=limit,
        )
        return [
            JobListRow(
                id=job.id,
                job_type=job.job_type,
                agent_id=job.agent_id,
                agent_name=agent.agent_name if agent else None,
                status=job.status,
                priority=job.priority,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                duration_seconds=job.duration_seconds,
                has_result=job.result is not None,
                has_error=job.error is not None,
                tags=job.tags,
            )
            for job, agent in rows
        ], total

    @staticmethod
    async def get_job_status_summary(db: AsyncSession) -> dict[str, Any]:
        """
        Get job status summary counts.

        Args:
            db: Database session

        Returns:
            Dictionary with total, per-status counts, and active count
        """
        return await JobCRUD.get_job_status_summary(db)
