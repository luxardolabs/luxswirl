"""
Job API core service - jobs in their JSON API shape.

Every JobResponse is built here, with the running agent's hostname resolved in
core, so no ORM job or agent reaches the router.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_model import Job
from app.schemas.job_schema import (
    JobCreate,
    JobResponse,
    JobResultSubmit,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.job_core_service import JobCoreService


class JobApiCoreService:
    """Jobs for the JSON API."""

    @staticmethod
    def to_response(job: Job, agent_hostname: str | None) -> JobResponse:
        """`job` in its API shape, labelled with its agent's hostname."""
        return JobResponse(
            id=job.id,
            job_type=job.job_type,
            agent_id=job.agent_id,
            agent_hostname=agent_hostname,
            params=job.params,
            priority=job.priority,
            status=job.status,
            tags=job.tags,
            created_at=job.created_at,
            updated_at=job.updated_at,
            assigned_at=job.assigned_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            expires_at=job.expires_at,
            duration_seconds=job.duration_seconds,
            result=job.result,
            error=job.error,
            created_by=job.created_by,
            schedule=job.schedule,
            automation_config=job.automation_config,
            parent_job_id=job.parent_job_id,
        )

    @staticmethod
    async def _with_hostname(db: AsyncSession, job: Job) -> JobResponse:
        hostnames = (
            await AgentCoreService.get_hostnames_by_ids(db, [job.agent_id])
            if job.agent_id
            else {}
        )
        return JobApiCoreService.to_response(
            job, hostnames.get(job.agent_id) if job.agent_id else None
        )

    @staticmethod
    async def create_job(
        db: AsyncSession, data: JobCreate, *, created_by: str | None = None
    ) -> JobResponse:
        """Create a job.

        Raises:
            AgentNotFoundException: If the job names an agent that does not exist
        """
        job = await JobCoreService.create_job(db, data, created_by=created_by)
        return await JobApiCoreService._with_hostname(db, job)

    @staticmethod
    async def get_job(db: AsyncSession, job_id: UUID) -> JobResponse | None:
        """One job, or None if there is no such job."""
        job = await JobCoreService.get_job(db, job_id)
        return None if job is None else await JobApiCoreService._with_hostname(db, job)

    @staticmethod
    async def submit_job_result(
        db: AsyncSession, job_id: UUID, data: JobResultSubmit
    ) -> JobResponse | None:
        """Record a job's result; None if there is no such job."""
        job = await JobCoreService.submit_job_result(db, job_id, data)
        return None if job is None else await JobApiCoreService._with_hostname(db, job)

    @staticmethod
    async def cancel_job(db: AsyncSession, job_id: UUID) -> bool:
        """Cancel a pending job or delete a finished one; False if neither applies."""
        return await JobCoreService.cancel_job(db, job_id) is not None
