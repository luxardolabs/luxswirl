"""
Check Result router - HTTP endpoints for check result operations.

All business logic is delegated to CheckResultCoreService.
This router only handles HTTP concerns.
"""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException, CheckNotFoundException
from app.core.security import verify_agent_token, verify_api_token
from app.db import get_db
from app.schemas.base import ErrorResponse
from app.schemas.check_result_schema import (
    AgentReportRequest,
    AgentReportResponse,
    CheckHistoryResponse,
    CheckResultListResponse,
    CheckResultResponse,
    CheckSummary,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_result_core_service import CheckResultCoreService

router = APIRouter(tags=["Check Results"])


@router.post(
    "/reports",
    response_model=AgentReportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit agent report",
    description="Submit check results from an agent",
)
async def submit_report(
    report: AgentReportRequest,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    """
    Submit a report from an agent containing multiple check results.

    This endpoint accepts reports and processes them asynchronously.

    This endpoint uses agent-specific authentication that checks approval status.
    """
    # Get agent by UUID
    agent = await AgentCoreService.get_agent_by_id(db, report.agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {report.agent_id}",
        )

    # Security: REQUIRE agent-specific key for check result submissions
    # Registration token is NOT allowed - prevents impersonation attacks
    if not agent.api_key_hash:
        # Agent hasn't been approved yet - can't submit check results
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent must be approved and have agent-specific key to submit check results",
        )

    # Verify agent-specific key (no fallback to registration token)
    await verify_agent_token(agent, authorization)

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
)
async def get_latest_results(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    minutes: Annotated[int, Query(ge=1, le=60, description="Minutes to look back")] = 5,
):
    """Get latest check results for an agent."""
    try:
        results = await CheckResultCoreService.get_latest_results_for_agent(db, agent_name, minutes)

        result_responses = []
        success_count = 0
        failure_count = 0

        for result in results:
            if result.success:
                success_count += 1
            else:
                failure_count += 1

            # Get check and agent info
            result_agent_name = result.agent.agent_name if result.agent else None
            result_check_name = result.check.display_name if result.check else None
            result_check_type = result.check.check_type if result.check else None
            result_target = result.check.target if result.check else None

            result_responses.append(
                CheckResultResponse(
                    id=result.id,
                    agent_id=result.agent_id,
                    check_id=result.check_id,
                    timestamp=result.timestamp,
                    success=result.success,
                    latency_ms=result.latency_ms,
                    latency_seconds=result.latency_seconds,
                    error=result.error,
                    error_type=result.error_type,
                    http_status_code=result.http_status_code,
                    http_response_time_ms=result.http_response_time_ms,
                    metrics=result.get_metrics(),
                    status=result.status,
                    agent_name=result_agent_name,
                    check_name=result_check_name,
                    check_type=result_check_type,
                    target=result_target,
                )
            )

        total = len(result_responses)
        success_rate = (success_count / total * 100) if total > 0 else 0.0

        return CheckResultListResponse(
            results=result_responses,
            total=total,
            success_count=success_count,
            failure_count=failure_count,
            success_rate=round(success_rate, 2),
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
)
async def get_check_history(
    agent_name: str,
    check_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[int, Query(ge=1, le=720, description="Hours of history")] = 24,
    limit: Annotated[int, Query(ge=1, le=10000, description="Max results")] = 1000,
):
    """Get historical check results."""
    try:
        results = await CheckResultCoreService.get_check_history(db, check_id, hours, limit)

        if not results:
            raise CheckNotFoundException("unknown", str(check_id))

        # Get summary statistics
        summary = await CheckResultCoreService.get_check_summary(db, check_id, hours)

        # Convert results to responses
        result_responses = []
        for result in results:
            result_agent_name = result.agent.agent_name if result.agent else None
            result_check_name = result.check.display_name if result.check else None
            result_check_type = result.check.check_type if result.check else None
            result_target = result.check.target if result.check else None

            result_responses.append(
                CheckResultResponse(
                    id=result.id,
                    agent_id=result.agent_id,
                    check_id=result.check_id,
                    timestamp=result.timestamp,
                    success=result.success,
                    latency_ms=result.latency_ms,
                    latency_seconds=result.latency_seconds,
                    error=result.error,
                    error_type=result.error_type,
                    http_status_code=result.http_status_code,
                    http_response_time_ms=result.http_response_time_ms,
                    metrics=result.get_metrics(),
                    status=result.status,
                    agent_name=result_agent_name,
                    check_name=result_check_name,
                    check_type=result_check_type,
                    target=result_target,
                )
            )

        # Get check info from first result
        first_result = results[0]
        agent_id = first_result.agent_id
        check_name = first_result.check.display_name if first_result.check else "unknown"
        check_type = first_result.check.check_type if first_result.check else "unknown"
        target = first_result.check.target if first_result.check else "unknown"

        end_time = utc_now()
        start_time = end_time - timedelta(hours=hours)

        return CheckHistoryResponse(
            agent_id=agent_id,
            check_name=check_name,
            check_type=check_type,
            target=target,
            start_time=start_time,
            end_time=end_time,
            data_points=result_responses,
            summary=summary,
        )
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


@router.get(
    "/agents/{agent_name}/checks/{check_id}/summary",
    response_model=CheckSummary,
    summary="Get check summary",
    description="Get summary statistics for a check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
)
async def get_check_summary(
    agent_name: str,
    check_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[int, Query(ge=1, le=720, description="Hours to include")] = 24,
):
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


@router.get(
    "/stats",
    summary="Get global statistics",
    description="Get aggregated statistics across all agents and checks",
)
async def get_global_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[int, Query(ge=1, le=720, description="Hours to include")] = 24,
):
    """Get global aggregated statistics."""
    return await CheckResultCoreService.get_aggregated_stats(db, hours)
