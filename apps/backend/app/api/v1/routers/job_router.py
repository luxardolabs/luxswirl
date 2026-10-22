"""
Job router - HTTP endpoints for job management.

All business logic is delegated to JobCoreService.
This router only handles HTTP concerns (request/response, status codes, etc.).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AgentOwnedJobId
from app.core.config import settings
from app.core.exceptions import AgentNotFoundException
from app.core.query_params import JobStatusFilter, JobTypeFilter
from app.core.rate_limit import limiter
from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.job_schema import (
    JobCreate,
    JobListResponse,
    JobResponse,
    JobResultSubmit,
    JobStatsResponse,
)
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.job_api_core_service import JobApiCoreService
from app.services.core.job_core_service import JobCoreService

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post(
    "",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new job",
    description="Create a job to be dispatched to an agent (or run on server)",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
    name="create_job",
)
async def create_job(
    data: JobCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    token: Annotated[str, Depends(verify_api_token)],
) -> JobResponse:
    """Create a new job."""
    try:
        # A FINGERPRINT, never the token. This used to store the raw bearer
        # token in `created_by` — a column described as "User who created the
        # job" and returned verbatim in JobResponse, so a valid API credential
        # was persisted in plaintext and served back to every reader of the job.
        # The JSON API has no user model, so a non-reversible label is the most
        # identity there is to record, and it still distinguishes one configured
        # token from another in an audit trail.
        created_by = f"api:{AuthCoreService.hash_token(token)[:12]}"

        return await JobApiCoreService.create_job(db, data, created_by=created_by)
    except AgentNotFoundException as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e


@router.get(
    "",
    response_model=JobListResponse,
    summary="List jobs",
    description="Get a list of jobs with optional filtering",
    name="list_jobs",
)
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Filter by agent ID")
    ] = None,
    status: JobStatusFilter = None,
    job_type: JobTypeFilter = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
) -> JobListResponse:
    """List all jobs with pagination and filtering."""
    return await JobApiCoreService.list_jobs(
        db,
        agent_filter=agent_filter,
        status=status,
        job_type=job_type,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get job by ID",
    description="Get detailed information about a specific job",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
    name="get_job",
)
async def get_job(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> JobResponse:
    """Get a specific job by ID."""
    job = await JobApiCoreService.get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )
    return job


@router.post(
    "/{job_id}/results",
    response_model=JobResponse,
    summary="Submit job results",
    description="Agent submits job execution results",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
    name="submit_job_results",
)
@limiter.limit(settings.security.agent_api_rate_limit)
async def submit_job_results(
    request: Request,
    owned_job_id: AgentOwnedJobId,
    result_data: JobResultSubmit,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> JobResponse:
    """Submit job results from the agent the job is assigned to."""
    job = await JobApiCoreService.submit_job_result(db, owned_job_id, result_data)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {owned_job_id}",
        )
    return job


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel/delete a job",
    description="Cancel a pending job or delete a completed job",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
    name="cancel_job",
)
async def cancel_job(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> None:
    """Cancel or delete a job."""
    if not await JobApiCoreService.cancel_job(db, job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found or cannot be cancelled: {job_id}",
        )


@router.get(
    "/stats/summary",
    response_model=JobStatsResponse,
    summary="Get job statistics",
    description="Get aggregated job statistics",
    name="get_job_stats",
)
async def get_job_stats(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Filter by agent ID")
    ] = None,
) -> JobStatsResponse:
    """Get job statistics."""
    agent_uuid, server_only = JobCoreService.resolve_runner_filter(agent_filter)
    return await JobCoreService.get_job_stats(
        db, agent_id=agent_uuid, server_only=server_only
    )
