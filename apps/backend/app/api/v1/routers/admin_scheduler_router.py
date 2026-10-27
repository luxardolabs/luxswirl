"""
Admin API scheduler router - the scheduled jobs, addressed by job key.

Every route takes an admin's token, as the web UI's scheduler page takes an
admin.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import ApiAdmin, require_scope
from app.core.config import settings
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.schemas.scheduler_schema import (
    SchedulerExecutionResponse,
    SchedulerJobResponse,
    SchedulerRunResult,
)
from app.services.core.admin_scheduler_core_service import AdminSchedulerCoreService

router = APIRouter(
    prefix="/scheduler/jobs",
    tags=["Admin API: Scheduler"],
    dependencies=[Depends(require_scope("scheduler", admin_writes=True))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=list[SchedulerJobResponse], name="admin_list_jobs")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_jobs(
    request: Request, principal: ApiAdmin
) -> list[SchedulerJobResponse]:
    """Every scheduled job, with its schedule and run statistics."""
    return await AdminSchedulerCoreService.list_jobs()


@router.get("/{job_key}", response_model=SchedulerJobResponse, name="admin_get_job")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_job(
    request: Request, job_key: str, principal: ApiAdmin
) -> SchedulerJobResponse:
    return await AdminSchedulerCoreService.get_job(job_key)


@router.get(
    "/{job_key}/history",
    response_model=list[SchedulerExecutionResponse],
    name="admin_get_job_history",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_job_history(
    request: Request,
    job_key: str,
    principal: ApiAdmin,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SchedulerExecutionResponse]:
    """The job's newest runs, newest first."""
    return await AdminSchedulerCoreService.get_history(job_key, limit)


@router.post(
    "/{job_key}/enable", response_model=SchedulerJobResponse, name="admin_enable_job"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_enable_job(
    request: Request, job_key: str, principal: ApiAdmin
) -> SchedulerJobResponse:
    """Turn the job on; it is scheduled from now."""
    return await AdminSchedulerCoreService.set_enabled(job_key, True)


@router.post(
    "/{job_key}/disable",
    response_model=SchedulerJobResponse,
    name="admin_disable_job",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_disable_job(
    request: Request, job_key: str, principal: ApiAdmin
) -> SchedulerJobResponse:
    """Turn the job off; it runs again only when turned on or run by hand."""
    return await AdminSchedulerCoreService.set_enabled(job_key, False)


@router.post("/{job_key}/run", response_model=SchedulerRunResult, name="admin_run_job")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_run_job(
    request: Request, job_key: str, principal: ApiAdmin
) -> SchedulerRunResult:
    """Run the job now and answer when it ends; a failed run is a `200` whose
    `status` is `failed`, with its `error`."""
    return await AdminSchedulerCoreService.run_job(job_key)


@router.post(
    "/{job_key}/reset", response_model=SchedulerJobResponse, name="admin_reset_job"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_reset_job(
    request: Request, job_key: str, principal: ApiAdmin
) -> SchedulerJobResponse:
    """Clear the job's retry count and failures, and turn it back on."""
    return await AdminSchedulerCoreService.reset_job(job_key)
