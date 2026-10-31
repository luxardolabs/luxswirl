"""
Check router - HTTP endpoints for check operations.

All business logic is delegated to CheckCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthenticatedAgentId
from app.core.openapi_operations import HandlerNamedRoute
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.check_schema import CheckListResponse
from app.services.core.check_api_core_service import CheckApiCoreService
from app.shared.logger import get_logger

# Agent-facing router (agents call these)
router = APIRouter(route_class=HandlerNamedRoute, tags=["Checks"])


logger = get_logger("luxswirl.api.checks")


@router.get(
    "/checks",
    response_model=CheckListResponse,
    summary="List checks for an agent",
    description="Get all checks configured for a specific agent",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
    name="api.agent.checks.index",
)
async def list_checks(
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
    return await CheckApiCoreService.check_list_for_agent(db, agent_id)
