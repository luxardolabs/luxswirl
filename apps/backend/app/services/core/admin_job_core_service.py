"""
Admin API job core service - agent jobs (network scan and discover), by id.

The job rules live in JobCoreService (params held to the type's schema, an
agent required) and JobToCheckCoreService (which hosts become which checks);
this service shapes them for the API and raises its typed errors.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    JobFinishedException,
    JobNotFoundException,
    ValidationException,
)
from app.models.enum_model import JobStatus, JobType, ScanCheckKind
from app.schemas.admin_job_schema import (
    AdminJobChecks,
    AdminJobCreate,
    JobChecksCreated,
    JobPage,
    JobTypeInfo,
)
from app.schemas.job_schema import JobCreate, JobResponse
from app.schemas.pagination_schema import build_pagination
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.job_api_core_service import JobApiCoreService
from app.services.core.job_core_service import JOB_CLASSES, JobCoreService
from app.services.core.job_to_check_core_service import (
    BulkCheckParams,
    JobToCheckCoreService,
)
from app.services.core.settings_core_service import SettingsCoreService

_CREATE_CHECKS = {
    ScanCheckKind.PING: JobToCheckCoreService.create_ping_checks_from_job,
    ScanCheckKind.WEB: JobToCheckCoreService.create_web_checks_from_job,
    ScanCheckKind.SSH: JobToCheckCoreService.create_ssh_checks_from_job,
    ScanCheckKind.DATABASE: JobToCheckCoreService.create_database_checks_from_job,
}


class AdminJobCoreService:
    """Agent jobs for the admin API."""

    @staticmethod
    async def list_jobs(
        db: AsyncSession,
        *,
        page: int,
        per_page: int,
        status: JobStatus | None = None,
        job_type: JobType | None = None,
        agent_id: UUID | None = None,
    ) -> JobPage:
        """One page of jobs, highest priority first, then newest."""
        jobs, total = await JobCoreService.list_jobs(
            db,
            agent_id=agent_id,
            status=status,
            job_type=job_type,
            offset=(page - 1) * per_page,
            limit=per_page,
        )
        hostnames = await AgentCoreService.get_hostnames_by_ids(
            db, [job.agent_id for job in jobs if job.agent_id]
        )
        return JobPage.of(
            [
                JobApiCoreService.to_response(
                    job, hostnames.get(job.agent_id) if job.agent_id else None
                )
                for job in jobs
            ],
            build_pagination(page=page, per_page=per_page, total=total),
        )

    @staticmethod
    def job_types() -> list[JobTypeInfo]:
        return [
            JobTypeInfo(
                job_type=job_type,
                display_name=job_class.display_name,
                description=job_class.display_description,
                requires_agent=job_class.requires_agent,
                timeout_seconds=job_class.default_timeout_seconds,
                params_schema=job_class.params_schema.model_json_schema(),
            )
            for job_type, job_class in JOB_CLASSES.items()
        ]

    @staticmethod
    async def get_job(db: AsyncSession, job_id: UUID) -> JobResponse:
        job = await JobApiCoreService.get_job(db, job_id)
        if job is None:
            raise JobNotFoundException(job_id)
        return job

    @staticmethod
    async def create_job(
        db: AsyncSession, username: str, data: AdminJobCreate
    ) -> JobResponse:
        """Queue a job for its agent, recorded as `username`'s."""
        return await JobApiCoreService.create_job(
            db, JobCreate(**data.model_dump()), created_by=username
        )

    @staticmethod
    async def cancel_job(db: AsyncSession, job_id: UUID) -> JobResponse:
        """Cancel an unfinished job.

        Raises:
            JobNotFoundException: If there is no such job
            JobFinishedException: If the job has already finished
        """
        await AdminJobCoreService.get_job(db, job_id)
        if not await JobApiCoreService.cancel_job(db, job_id):
            raise JobFinishedException(job_id)
        return await AdminJobCoreService.get_job(db, job_id)

    @staticmethod
    async def delete_job(db: AsyncSession, job_id: UUID) -> None:
        await JobCoreService.delete_job(db, job_id)

    @staticmethod
    async def create_checks(
        db: AsyncSession, job_id: UUID, data: AdminJobChecks
    ) -> JobChecksCreated:
        """Create one kind of check from a scan's hosts.

        Raises:
            JobNotFoundException: If there is no such job
            ValidationException: If the job has no scan result to create from
        """
        await AdminJobCoreService.get_job(db, job_id)
        defaults = await SettingsCoreService.get_check_defaults(db)
        params = BulkCheckParams(
            interval=(
                defaults.interval_seconds
                if data.interval_seconds is None
                else data.interval_seconds
            ),
            timeout=(
                defaults.timeout_seconds
                if data.timeout_seconds is None
                else data.timeout_seconds
            ),
            retry_attempts=(
                defaults.retry_attempts
                if data.retry_attempts is None
                else data.retry_attempts
            ),
            agent_id=data.agent_id,
            tags=data.tags,
            expected_status=(
                defaults.expected_status
                if data.expected_status is None
                else data.expected_status
            ),
            verify_ssl=(
                defaults.verify_ssl if data.verify_ssl is None else data.verify_ssl
            ),
        )
        summary, error = await _CREATE_CHECKS[data.kind](db, job_id, params)
        if summary is None:
            raise ValidationException(error or "The job has no scan result")
        return JobChecksCreated(
            created=summary.created_count, skipped=summary.skipped_count
        )
