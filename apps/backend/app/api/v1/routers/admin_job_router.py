"""
Admin API job router - agent jobs (network scan and discover), by id.

Reads take any API token; creating, cancelling and deleting a job, and
creating checks from a scan, take an editor's or an admin's, as on the web
jobs page.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser, require_scope, require_scopes
from app.core.config import settings
from app.core.query_params import AgentIdFilter, JobStatusFilter, JobTypeFilter
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_job_schema import (
    AdminJobChecks,
    AdminJobCreate,
    JobChecksCreated,
    JobPage,
    JobTypeInfo,
)
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.job_schema import JobResponse
from app.services.core.admin_job_core_service import AdminJobCoreService

router = APIRouter(
    prefix="/jobs",
    tags=["Admin API: Jobs"],
    dependencies=[Depends(require_scope("jobs"))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=JobPage, name="admin_list_agent_jobs")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_agent_jobs(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    status: JobStatusFilter = None,
    job_type: JobTypeFilter = None,
    agent_id: AgentIdFilter = None,
) -> JobPage:
    """One page of jobs, highest priority first, then newest."""
    return await AdminJobCoreService.list_jobs(
        db,
        page=page,
        per_page=per_page,
        status=status,
        job_type=job_type,
        agent_id=agent_id,
    )


@router.get("/types", response_model=list[JobTypeInfo], name="admin_list_job_types")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_job_types(
    request: Request, principal: ApiUser
) -> list[JobTypeInfo]:
    """Every job type, with the JSON Schema of the params it takes."""
    return AdminJobCoreService.job_types()


@router.post(
    "",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    name="admin_create_agent_job",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_agent_job(
    request: Request,
    data: AdminJobCreate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> JobResponse:
    """Queue a job; its agent picks it up on its next heartbeat."""
    return await AdminJobCoreService.create_job(db, principal.user.username, data)


@router.get("/{job_id}", response_model=JobResponse, name="admin_get_agent_job")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_agent_job(
    request: Request,
    job_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> JobResponse:
    """One job, with its result once it has finished."""
    return await AdminJobCoreService.get_job(db, job_id)


@router.post(
    "/{job_id}/cancel", response_model=JobResponse, name="admin_cancel_agent_job"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_cancel_agent_job(
    request: Request,
    job_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> JobResponse:
    """Cancel a job that has not finished; a finished one is a 409."""
    return await AdminJobCoreService.cancel_job(db, job_id)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    name="admin_delete_agent_job",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_agent_job(
    request: Request,
    job_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    await AdminJobCoreService.delete_job(db, job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{job_id}/checks",
    response_model=JobChecksCreated,
    status_code=status.HTTP_201_CREATED,
    name="admin_create_checks_from_job",
    dependencies=[Depends(require_scopes("checks:write"))],
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_create_checks_from_job(
    request: Request,
    job_id: UUID,
    data: AdminJobChecks,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> JobChecksCreated:
    """Create one kind of check from a scan's hosts; one that exists is skipped."""
    return await AdminJobCoreService.create_checks(db, job_id, data)
