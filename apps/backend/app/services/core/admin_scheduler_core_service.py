"""
Admin scheduler core service - the scheduled jobs as the admin API serves them.

An admin's alone, as in the web UI. The scheduler owns its own sessions, so
each call commits on its own.
"""

from app.schemas.scheduler_schema import (
    SchedulerExecutionResponse,
    SchedulerJobResponse,
    SchedulerRunResult,
)
from app.services.core.scheduler_core_service import scheduler_service


class AdminSchedulerCoreService:
    """List, read, turn on or off, run and reset scheduled jobs."""

    @staticmethod
    async def list_jobs() -> list[SchedulerJobResponse]:
        return await scheduler_service.get_all_jobs()

    @staticmethod
    async def get_job(job_key: str) -> SchedulerJobResponse:
        return await scheduler_service.get_job(job_key)

    @staticmethod
    async def get_history(job_key: str, limit: int) -> list[SchedulerExecutionResponse]:
        """The job's newest runs, newest first."""
        _, executions = await scheduler_service.get_job_history(job_key, limit=limit)
        return executions

    @staticmethod
    async def set_enabled(job_key: str, enabled: bool) -> SchedulerJobResponse:
        return await scheduler_service.set_job_enabled(job_key, enabled)

    @staticmethod
    async def run_job(job_key: str) -> SchedulerRunResult:
        """Run the job now and wait for it; a failed run is returned, not raised."""
        return await scheduler_service.execute_job_synchronously(job_key)

    @staticmethod
    async def reset_job(job_key: str) -> SchedulerJobResponse:
        """Clear the job's failures and turn it back on."""
        return await scheduler_service.reset_job(job_key)
