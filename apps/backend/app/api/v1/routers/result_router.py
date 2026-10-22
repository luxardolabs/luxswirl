"""
Check Result router - HTTP endpoints for check result operations.

All business logic is delegated to CheckResultCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ReportingAgentId
from app.core.config import settings
from app.core.exceptions import AgentNotFoundException, CheckNotFoundException
from app.core.rate_limit import limiter
from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.check_result_schema import (
    AgentReportRequest,
    AgentReportResponse,
    CheckHistoryResponse,
    CheckResultListResponse,
    CheckSummary,
    GlobalStatsResponse,
)
from app.services.core.check_result_api_core_service import CheckResultApiCoreService
from app.services.core.check_result_core_service import CheckResultCoreService

router = APIRouter(tags=["Check Results"])


@router.post(
    "/reports",
    response_model=AgentReportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit agent report",
    description="Submit check results from an agent",
    name="submit_report",
)
@limiter.limit(settings.security.agent_api_rate_limit)
async def submit_report(
    request: Request,
    report: AgentReportRequest,
    _agent_id: ReportingAgentId,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentReportResponse:
    """
    Submit a report from an agent containing multiple check results.

    This endpoint accepts reports and processes them asynchronously.

    Only the agent's own key is accepted (`ReportingAgentId`), never the
    registration key.
    """
    # Process report (this commits the transaction)
    result = await CheckResultCoreService.process_agent_report(db, report)

    return AgentReportResponse(
        status=result["status"],
        agent_id=result["agent_id"],
        received_at=result["received_at"],
        results_processed=result["results_processed"],
        results_failed=result.get("results_failed", 0),
    )


@router.get(
    "/agents/{agent_name}/results",
    response_model=CheckResultListResponse,
    summary="Get latest results for agent",
    description="Get the latest check results for all checks of an agent",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
    name="get_latest_results",
)
async def get_latest_results(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    minutes: Annotated[int, Query(ge=1, le=60, description="Minutes to look back")] = 5,
) -> CheckResultListResponse:
    """Get latest check results for an agent."""
    try:
        return await CheckResultApiCoreService.latest_results_for_agent(
            db, agent_name, minutes
        )
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_name}",
        ) from None


@router.get(
    "/agents/{agent_name}/checks/{check_id}/history",
    response_model=CheckHistoryResponse,
    summary="Get check history",
    description="Get historical check results for a specific check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
    name="get_check_history",
)
async def get_check_history(
    agent_name: str,
    check_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[int, Query(ge=1, le=720, description="Hours of history")] = 24,
    limit: Annotated[int, Query(ge=1, le=10000, description="Max results")] = 1000,
) -> CheckHistoryResponse:
    """Get historical check results."""
    try:
        return await CheckResultApiCoreService.check_history(
            db, agent_name, check_id, hours, limit
        )
    except CheckNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {agent_name}:{check_id}",
        ) from None


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/agents/{agent_name}/checks/{check_id}/summary",
    response_model=CheckSummary,
    summary="Get check summary",
    description="Get summary statistics for a check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
    name="get_check_summary",
)
async def get_check_summary(
    agent_name: str,
    check_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[int, Query(ge=1, le=720, description="Hours to include")] = 24,
) -> CheckSummary:
    """Get summary statistics for a check."""
    try:
        return await CheckResultCoreService.get_check_summary(db, check_id, hours)
    except CheckNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {agent_name}:{check_id}",
        ) from None
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_name}",
        ) from None


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/stats",
    summary="Get global statistics",
    description="Get aggregated statistics across all agents and checks",
    response_model=GlobalStatsResponse,
    name="get_global_stats",
)
async def get_global_stats(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[int, Query(ge=1, le=720, description="Hours to include")] = 24,
) -> GlobalStatsResponse:
    """Get global aggregated statistics."""
    return await CheckResultCoreService.get_aggregated_stats(db, hours)
