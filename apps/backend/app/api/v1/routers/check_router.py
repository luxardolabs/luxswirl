"""
Check router - HTTP endpoints for check operations.

All business logic is delegated to CheckCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthenticatedAgentId
from app.core.check_target_validator import CheckTargetBlockedError
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.check_schema import (
    BulkCheckCreateRequest,
    BulkCheckCreateResponse,
    CheckCreate,
    CheckListResponse,
    CheckResponse,
    CheckUpdate,
)
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.check_api_core_service import CheckApiCoreService

# Agent-facing router (agents call these)
router = APIRouter(tags=["Checks"])

# Management router (web UI / users call these)
management_router = APIRouter(
    prefix="/agents/{agent_id}/checks", tags=["Checks - Management"]
)

logger = get_logger("luxswirl.api.checks")


@router.get(
    "/checks",
    response_model=CheckListResponse,
    summary="List checks for an agent",
    description="Get all checks configured for a specific agent",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
)
@limiter.limit(settings.security.agent_api_rate_limit)
async def list_checks(
    request: Request,
    agent_id: AuthenticatedAgentId,
    db: Annotated[AsyncSession, Depends(get_db)],
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


@management_router.get(
    "/{check_id}",
    response_model=CheckResponse,
    summary="Get a specific check",
    description="Get detailed information about a specific check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
)
async def get_check(
    agent_id: UUID,
    check_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckResponse:
    """Get a specific check."""
    return await CheckApiCoreService.get_check(db, agent_id, check_id)


@management_router.post(
    "",
    response_model=CheckResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new check",
    description="Create a new check for an agent",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
)
async def create_check(
    agent_id: UUID,
    data: CheckCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckResponse:
    """
    Create a new check.

    Security Note: API tokens have admin-level access. Synthetic checks execute arbitrary
    Python code and are restricted to administrators. Use with caution in trusted environments only.
    """
    try:
        return await CheckApiCoreService.create_check(db, agent_id, data)
    except CheckTargetBlockedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from None


@management_router.patch(
    "/{check_id}",
    response_model=CheckResponse,
    summary="Update a check",
    description="Update check configuration",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
)
async def update_check(
    agent_id: UUID,
    check_id: UUID,
    data: CheckUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckResponse:
    """Update a check."""
    try:
        return await CheckApiCoreService.update_check(db, agent_id, check_id, data)
    except CheckTargetBlockedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from None


@management_router.delete(
    "/{check_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a check",
    description="Delete a check and all associated results",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
)
async def delete_check(
    agent_id: UUID,
    check_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> None:
    """Delete a check."""
    await CheckApiCoreService.delete_check(db, agent_id, check_id)


@management_router.post(
    "/{check_id}/clone",
    response_model=CheckResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Clone an existing check",
    description="Create a new check by cloning an existing one with optional field overrides",
    responses={
        404: {"model": ErrorResponse, "description": "Check or agent not found"},
    },
)
async def clone_check(
    agent_id: UUID,
    check_id: UUID,
    data: CheckUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckResponse:
    """
    Clone an existing check to a target agent.

    All fields from the source check are copied to the new check. You can override
    specific fields by including them in the request body. Fields not included in
    the request body will use the source check's values.

    Example request:
    ```json
    {
        "display_name": "api_health-clone",
        "enabled": false,
        "interval_seconds": 120
    }
    ```

    All other fields will be copied from the source check.
    """
    return await CheckApiCoreService.clone_check(db, agent_id, check_id, data)


@management_router.post(
    "/bulk",
    response_model=BulkCheckCreateResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk create checks from URLs",
    description="Create multiple checks at once from a list of URLs with automatic type detection and name generation",
    responses={
        404: {"model": ErrorResponse, "description": "Agent not found"},
    },
)
async def bulk_create_checks(
    agent_id: UUID,
    requests: list[BulkCheckCreateRequest],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> BulkCheckCreateResponse:
    """
    Bulk create checks from a list of URLs.

    Features:
    - Automatic check type detection (http, https, tcp)
    - Auto-generated display names from URLs if not provided
    - Partial success support (some checks can succeed while others fail)
    - Detailed error reporting per check

    Example request:
    ```json
    [
        {
            "url": "https://api.example.com/health",
            "interval_seconds": 60,
            "enabled": true,
            "tags": ["production"]
        },
        {
            "url": "https://www.example.com",
            "display_name": "example-homepage",
            "interval_seconds": 120
        },
        {
            "url": "tcp://db.example.com:5432"
        }
    ]
    ```

    Returns detailed results for each check including success/failure status.
    """
    return await CheckApiCoreService.bulk_create_checks(db, agent_id, requests)
