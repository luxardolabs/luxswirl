"""
Check Result router - HTTP endpoints for check result operations.

All business logic is delegated to CheckResultCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Request,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ReportingAgentId
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db import get_db
from app.schemas.check_result_schema import (
    AgentReportRequest,
    AgentReportResponse,
)
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
