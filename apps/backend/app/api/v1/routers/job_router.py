"""
Job router - HTTP endpoints for job management.

All business logic is delegated to JobCoreService.
This router only handles HTTP concerns (request/response, status codes, etc.).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AgentNotFoundException
from app.core.query_params import JobStatusFilter, JobTypeFilter
from app.core.security import verify_agent_token, verify_api_token
from app.db import get_db
from app.schemas.base import ErrorResponse
from app.schemas.job_schema import (
    JobCreate,
    JobListResponse,
    JobResponse,
    JobResultSubmit,
)
from app.services.core.agent_core_service import AgentCoreService
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
)
async def create_job(
    data: JobCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
):
    """Create a new job."""
    try:
        # Extract user from token if needed (for now, use token as identifier)
        created_by = token if token != "changeme" else None

        job = await JobCoreService.create_job(db, data, created_by=created_by)

        # Get agent hostname if applicable
        agent_hostname = None
        if job.agent_id:
            agent = await AgentCoreService.get_agent_by_id(db, job.agent_id)
            if agent:
                agent_hostname = agent.hostname

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
)
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Filter by agent ID")
    ] = None,
    status: JobStatusFilter = None,
    job_type: JobTypeFilter = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
):
    """List all jobs with pagination and filtering."""
    offset = (page - 1) * page_size
    agent_uuid, server_only = JobCoreService.resolve_runner_filter(agent_filter)

    jobs, total = await JobCoreService.list_jobs(
        db=db,
        agent_id=agent_uuid,
        server_only=server_only,
        status=status,
        job_type=job_type,
        offset=offset,
        limit=page_size,
    )

    # Get agent hostnames for jobs
    agent_cache = {}

    job_responses = []
    for job in jobs:
        agent_hostname = None
        if job.agent_id and job.agent_id not in agent_cache:
            agent = await AgentCoreService.get_agent_by_id(db, job.agent_id)
            agent_cache[job.agent_id] = agent.hostname if agent else None

        if job.agent_id:
            agent_hostname = agent_cache.get(job.agent_id)

        job_responses.append(
            JobResponse(
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
        )

    # Get stats
    stats = await JobCoreService.get_job_stats(db, agent_id=agent_uuid, server_only=server_only)

    return JobListResponse(
        jobs=job_responses,
        total=total,
        pending_count=stats.get("pending", 0),
        running_count=stats.get("running", 0),
        completed_count=stats.get("completed", 0),
        failed_count=stats.get("failed", 0),
    )


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get job by ID",
    description="Get detailed information about a specific job",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
)
async def get_job(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Get a specific job by ID."""
    job = await JobCoreService.get_job(db, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )

    # Get agent hostname if applicable
    agent_hostname = None
    if job.agent_id:
        agent = await AgentCoreService.get_agent_by_id(db, job.agent_id)
        if agent:
            agent_hostname = agent.hostname

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


@router.post(
    "/{job_id}/results",
    response_model=JobResponse,
    summary="Submit job results",
    description="Agent submits job execution results",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
)
async def submit_job_results(
    job_id: UUID,
    result_data: JobResultSubmit,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    """Submit job results from agent."""
    job = await JobCoreService.submit_job_result(db, job_id, result_data)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found: {job_id}",
        )

    # Get agent and verify authentication
    agent_hostname = None
    if job.agent_id:
        agent = await AgentCoreService.get_agent_by_id(db, job.agent_id)
        if agent:
            agent_hostname = agent.hostname

            # Security: REQUIRE agent-specific key for job submissions
            # Registration token is NOT allowed - prevents impersonation attacks

            if not agent.api_key_hash:
                # Agent hasn't been approved yet - can't submit job results
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Agent must be approved and have agent-specific key to submit job results",
                )

            # Verify agent-specific key (no fallback to registration token)
            await verify_agent_token(agent, authorization)

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


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel/delete a job",
    description="Cancel a pending job or delete a completed job",
    responses={
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
)
async def cancel_job(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Cancel or delete a job."""
    job = await JobCoreService.cancel_job(db, job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job not found or cannot be cancelled: {job_id}",
        )

    return None


@router.get(
    "/stats/summary",
    response_model=dict,
    summary="Get job statistics",
    description="Get aggregated job statistics",
)
async def get_job_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    agent_filter: Annotated[
        str | None, Query(alias="agent_id", description="Filter by agent ID")
    ] = None,
):
    """Get job statistics."""
    agent_uuid, server_only = JobCoreService.resolve_runner_filter(agent_filter)
    return await JobCoreService.get_job_stats(db, agent_id=agent_uuid, server_only=server_only)
