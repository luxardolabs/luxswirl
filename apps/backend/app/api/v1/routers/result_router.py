"""
Check Result router - HTTP endpoints for check result operations.

All business logic is delegated to CheckResultCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ReportingAgentId
from app.core.agent_refusals import AgentProtocolRoute
from app.db import get_db
from app.schemas.check_result_schema import (
    AgentReportResponse,
    AgentReportSubmission,
)
from app.services.core.check_result_core_service import CheckResultCoreService

router = APIRouter(route_class=AgentProtocolRoute, tags=["Check Results"])


@router.post(
    "/reports",
    response_model=AgentReportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit agent report",
    description="Submit check results from an agent",
    name="api.agent.reports.create",
)
async def submit_report(
    report: AgentReportSubmission,
    agent: ReportingAgentId,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentReportResponse:
    """
    Submit a report from an agent containing multiple check results.

    The results are stored, and their alerts decided, in the request's
    transaction; each notification decided is queued in it and delivered by
    the delivery worker once it commits, so the answer never waits on a
    notification provider.

    Only the agent's own key is accepted (`ReportingAgentId`), never the
    registration key. Each result is validated on its own: an invalid one is
    counted in `results_failed`, and the rest are stored.
    """
    result = await CheckResultCoreService.process_agent_submission(
        db, report, verified_key_hash=agent.key_hash
    )

    return AgentReportResponse(
        status=result["status"],
        agent_id=result["agent_id"],
        received_at=result["received_at"],
        results_processed=result["results_processed"],
        results_failed=result.get("results_failed", 0),
    )
