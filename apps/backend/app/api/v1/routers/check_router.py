"""
Check router - HTTP endpoints for check operations.

All business logic is delegated to CheckCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.check_target_validator import CheckTargetBlockedError
from app.core.exceptions import (
    AgentNotFoundException,
    CheckNotFoundException,
)
from app.core.security import verify_agent_token, verify_api_token
from app.db import get_db
from app.schemas.base import ErrorResponse
from app.schemas.check_schema import (
    BulkCheckCreateRequest,
    BulkCheckCreateResponse,
    CheckCreate,
    CheckListResponse,
    CheckResponse,
    CheckUpdate,
)
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService

# Agent-facing router (agents call these)
router = APIRouter(tags=["Checks"])

# Management router (web UI / users call these)
management_router = APIRouter(prefix="/agents/{agent_id}/checks", tags=["Checks - Management"])

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
async def list_checks(
    agent_id: Annotated[UUID, Query(description="Agent UUID to fetch checks for")],
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    """
    List all checks for an agent.

    Returns checks based on assignment mode:
    - MANUAL: Checks explicitly assigned to this agent
    - REPLICATE: Checks that match this agent's tags (runs on ALL matching agents)
    - DISTRIBUTE: Checks assigned via hash (runs on ONE agent from matching pool)

    This endpoint uses agent-specific authentication that checks approval status.
    """
    # Get agent
    agent = await AgentCoreService.get_agent_by_id(db, agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        )

    # Verify agent authentication and approval status
    await verify_agent_token(agent, authorization)
    try:
        # Get checks using assignment service (handles manual/replicate/distribute)
        checks = await AgentAssignmentCoreService.get_checks_for_agent(db, agent)

        # Build response using service
        return CheckCoreService.build_check_list_response(checks, agent.agent_name)
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None


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
):
    """Get a specific check."""
    try:
        check = await CheckCoreService.get_check_by_id(db, check_id)
        # Bind the lookup to the {agent_id} in the path — a check under another agent
        # must 404 here, not resolve via this agent's URL (LUXSWIRL-190 L-5).
        if not check or check.agent_id != agent_id:
            raise CheckNotFoundException("unknown", str(check_id))

        # Get agent for FQN
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        fqn = f"{agent.agent_name or str(agent.id)}:{check.display_name}"

        return CheckResponse(
            id=check.id,
            agent_id=check.agent_id,
            display_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            description=check.description,
            interval_seconds=check.interval_seconds,
            timeout_seconds=check.timeout_seconds,
            expected_status=check.expected_status,
            enabled=check.enabled,
            created_at=check.created_at,
            updated_at=check.updated_at,
            fully_qualified_name=fqn,
            latest_status=None,
            latest_latency_ms=None,
            success_rate_24h=None,
        )
    except CheckNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        ) from None
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None


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
):
    """
    Create a new check.

    Security Note: API tokens have admin-level access. Synthetic checks execute arbitrary
    Python code and are restricted to administrators. Use with caution in trusted environments only.
    """
    try:
        # Get agent by UUID to get agent_name
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # API Bearer tokens are admin-equivalent by design (see SECURITY.md), so
        # synthetic checks are permitted on this path.
        check = await CheckCoreService.create_check(db, agent.id, data, actor_is_admin=True)

        fqn = f"{agent.agent_name or str(agent.id)}:{check.display_name}"

        return CheckResponse(
            id=check.id,
            agent_id=check.agent_id,
            display_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            description=check.description,
            interval_seconds=check.interval_seconds,
            timeout_seconds=check.timeout_seconds,
            expected_status=check.expected_status,
            enabled=check.enabled,
            created_at=check.created_at,
            updated_at=check.updated_at,
            fully_qualified_name=fqn,
            latest_status=None,
            latest_latency_ms=None,
            success_rate_24h=None,
        )
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None
    except CheckTargetBlockedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
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
):
    """Update a check."""
    try:
        # Bind {check_id} to the path {agent_id} before mutating (LUXSWIRL-190 L-5).
        existing = await CheckCoreService.get_check_by_id(db, check_id)
        if existing.agent_id != agent_id:
            raise CheckNotFoundException("unknown", str(check_id))
        check = await CheckCoreService.update_check(db, check_id, data, actor_is_admin=True)

        # Get agent for FQN
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        fqn = f"{agent.agent_name or str(agent.id)}:{check.display_name}"

        return CheckResponse(
            id=check.id,
            agent_id=check.agent_id,
            display_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            description=check.description,
            interval_seconds=check.interval_seconds,
            timeout_seconds=check.timeout_seconds,
            expected_status=check.expected_status,
            enabled=check.enabled,
            created_at=check.created_at,
            updated_at=check.updated_at,
            fully_qualified_name=fqn,
            latest_status=None,
            latest_latency_ms=None,
            success_rate_24h=None,
        )
    except CheckNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        ) from None
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None
    except CheckTargetBlockedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
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
):
    """Delete a check."""
    try:
        # Bind {check_id} to the path {agent_id} before deleting (LUXSWIRL-190 L-5).
        existing = await CheckCoreService.get_check_by_id(db, check_id)
        if existing.agent_id != agent_id:
            raise CheckNotFoundException("unknown", str(check_id))
        await CheckCoreService.delete_check(db, check_id)
        return None
    except CheckNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        ) from None


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
    data: CheckCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
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
    try:
        # Check if any overrides were provided
        has_overrides = any(data.model_dump(exclude_unset=True).values())

        # Clone check with overrides
        cloned_check = await CheckCoreService.clone_check(
            db,
            source_check_id=check_id,
            target_agent_id=agent_id,
            overrides=data if has_overrides else None,
            actor_is_admin=True,
        )

        # Get agent for FQN
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        fqn = f"{agent.agent_name or str(agent.id)}:{cloned_check.display_name}"

        return CheckResponse(
            id=cloned_check.id,
            agent_id=cloned_check.agent_id,
            display_name=cloned_check.display_name,
            check_type=cloned_check.check_type,
            target=cloned_check.target,
            description=cloned_check.description,
            interval_seconds=cloned_check.interval_seconds,
            timeout_seconds=cloned_check.timeout_seconds,
            expected_status=cloned_check.expected_status,
            enabled=cloned_check.enabled,
            created_at=cloned_check.created_at,
            updated_at=cloned_check.updated_at,
            fully_qualified_name=fqn,
            latest_status=None,
            latest_latency_ms=None,
            success_rate_24h=None,
        )
    except CheckNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        ) from None
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None


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
):
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
    try:
        # Verify agent exists
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # Bulk create checks
        result = await CheckCoreService.bulk_create_checks(db, agent.id, requests)

        # Commit the transaction

        return BulkCheckCreateResponse(
            total=result["total"],
            succeeded=result["succeeded"],
            failed=result["failed"],
            results=result["results"],
        )

    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None
