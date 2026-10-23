"""
Job router - HTTP endpoint for agent job results.

All business logic is delegated to JobCoreService.
This router only handles HTTP concerns (request/response, status codes, etc.).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AgentOwnedJobId
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.job_schema import (
    JobResponse,
    JobResultSubmit,
)
from app.services.core.job_api_core_service import JobApiCoreService

router = APIRouter(prefix="/jobs", tags=["Jobs"])


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
