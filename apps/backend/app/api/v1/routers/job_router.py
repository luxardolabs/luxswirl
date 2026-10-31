"""
Job router - HTTP endpoints for an agent's own jobs: started, and results.

All business logic is delegated to JobCoreService.
This router only handles HTTP concerns (request/response, status codes, etc.).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AgentOwnedJobId
from app.core.openapi_operations import HandlerNamedRoute
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.job_schema import (
    JobResponse,
    JobResultSubmit,
)
from app.services.core.job_api_core_service import JobApiCoreService

router = APIRouter(route_class=HandlerNamedRoute, prefix="/jobs", tags=["Jobs"])


@router.post(
    "/{job_id}/start",
    response_model=JobResponse,
    summary="Report a job started",
    description="Agent reports it has begun running the job (it becomes running)",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
    name="api.agent.jobs.start",
)
async def start_job(
    owned_job_id: AgentOwnedJobId,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> JobResponse:
    """Mark the job running, for the agent the job is assigned to."""
    job = await JobApiCoreService.mark_job_started(db, owned_job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {owned_job_id}",
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
    name="api.agent.job_results.create",
)
async def submit_job_results(
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
