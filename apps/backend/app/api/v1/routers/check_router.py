"""
Check router - HTTP endpoints for check operations.

All business logic is delegated to CheckCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthenticatedAgentId
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.check_schema import CheckListResponse
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService

# Agent-facing router (agents call these)
router = APIRouter(tags=["Checks"])


logger = get_logger("luxswirl.api.checks")


@router.get(
    "/checks",
    response_model=CheckListResponse,
    summary="List checks for an agent",
    description="Get all checks configured for a specific agent",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
    name="list_checks",
)
@limiter.limit(settings.security.agent_api_rate_limit)
async def list_checks(
    request: Request,
    agent_id: AuthenticatedAgentId,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CheckListResponse:
    """
    List all checks for an agent.

    Returns checks based on assignment mode:
    - MANUAL: Checks explicitly assigned to this agent
    - REPLICATE: Checks that match this agent's tags (runs on ALL matching agents)
    - DISTRIBUTE: Checks assigned via hash (runs on ONE agent from matching pool)

    The agent authenticates with its own key and must be approved
    (`AuthenticatedAgentId`).
    """
    return await AgentAssignmentCoreService.check_list_for_agent(db, agent_id)
