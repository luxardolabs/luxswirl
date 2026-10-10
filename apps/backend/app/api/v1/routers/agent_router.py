"""
Agent router - HTTP endpoints for agent operations.

All business logic lives in AgentCoreService. This module only handles HTTP
concerns: request/response, status codes, auth dependencies. The global
LuxSwirlException handler in main.py converts AgentNotFoundException → 404,
so routes do not catch it explicitly.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthenticatedHeartbeat
from app.core.config import settings
from app.core.exceptions import AgentNotFoundException
from app.core.rate_limit import limiter
from app.core.security import (
    verify_api_token,
    verify_registration_token,
)
from app.db import get_db
from app.models.enum_model import AgentApprovalStatus, MaintenanceJobKind
from app.schemas.agent_schema import (
    AgentCreate,
    AgentHeartbeat,
    AgentHeartbeatResponse,
    AgentKeyIssuedResponse,
    AgentLifecycleResponse,
    AgentListResponse,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentResponse,
    AgentStatsResponse,
    AgentUpdate,
)
from app.schemas.base_schema import ErrorResponse
from app.schemas.job_schema import MaintenanceJobEnqueuedResponse
from app.schemas.registration_key_schema import (
    AgentKeyRecoveryResponse,
    AgentKeyRegenerateResponse,
)
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

logger = get_logger("luxswirl.agent_router")

# Agent-facing router (agents call these endpoints)
agent_ops_router = APIRouter(tags=["Agent Operations"])

# Management router (web UI / users call these)
router = APIRouter(prefix="/agents", tags=["Agents"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Agent not found"}
}


@agent_ops_router.post(
    "/agents/register",
    response_model=AgentRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new agent",
    description="Register a new agent for approval using registration key. Returns agent UUID to store locally.",
)
@limiter.limit(settings.security.registration_rate_limit)
async def register_agent(
    request: Request,
    data: AgentRegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> AgentRegisterResponse:
    await verify_registration_token(authorization, db)
    agent = await AgentCoreService.register_agent(
        db=db,
        hostname=data.hostname or "",
        ip_address=data.ip_address,
        version=data.version,
        tags=data.tags,
    )
    return AgentRegisterResponse(
        agent_id=agent.id,
        status=AgentApprovalStatus.PENDING,
        message="Agent registered successfully. Awaiting administrator approval.",
    )


@router.get(
    "",
    response_model=AgentListResponse,
    summary="List all agents",
    description="Get a list of all agents with optional filtering by active status",
)
async def list_agents(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    active_only: Annotated[
        bool, Query(description="Only return active agents")
    ] = False,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
) -> AgentListResponse:
    return await AgentCoreService.list_agents_with_stats(
        db=db,
        active_only=active_only,
        offset=(page - 1) * page_size,
        limit=page_size,
    )


@router.get(
    "/{agent_name}",
    response_model=AgentResponse,
    summary="Get agent by name",
    responses=_NOT_FOUND,
)
async def get_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentResponse:
    return await AgentCoreService.get_agent_response_by_name(db, agent_name)


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new agent",
    responses={409: {"model": ErrorResponse, "description": "Agent already exists"}},
)
async def create_agent(
    data: AgentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentResponse:
    return await AgentCoreService.create_agent_response(db, data)


@router.patch(
    "/{agent_id}",
    response_model=AgentResponse,
    summary="Update an agent",
    responses=_NOT_FOUND,
)
async def update_agent(
    agent_id: UUID,
    data: AgentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentResponse:
    return await AgentCoreService.update_agent_response(db, agent_id, data)


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue agent delete (async — runs in maintenance worker)",
    responses=_NOT_FOUND,
    response_model=MaintenanceJobEnqueuedResponse,
)
async def delete_agent(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> MaintenanceJobEnqueuedResponse:
    """Enqueue an agent_delete maintenance job and return the job id.

    The cascade through checks → check_results runs in the in-process worker
    so this request never holds a long transaction. Poll
    `GET /maintenance/{job_id}/status` (web) for completion, or query the
    `maintenance_jobs` table directly. See LUXSWIRL-105.
    """
    # 404 if the agent doesn't exist
    await AgentCoreService.get_agent_by_id(db, agent_id)
    job = await MaintenanceJobCoreService.enqueue(
        db,
        kind=MaintenanceJobKind.AGENT_DELETE,
        target_id=agent_id,
    )
    return MaintenanceJobEnqueuedResponse.model_validate(job)


@router.post(
    "/{agent_name}/approve",
    response_model=AgentKeyIssuedResponse,
    summary="Approve pending agent",
    responses=_NOT_FOUND,
)
async def approve_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentKeyIssuedResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    _approved_agent, api_key = await AgentCoreService.approve_agent(db, agent_id)
    return AgentKeyIssuedResponse(
        message="Agent approved successfully",
        agent_name=agent_name,
        api_key=api_key,
        status=AgentApprovalStatus.ACTIVE,
    )


@router.post(
    "/{agent_name}/reject",
    response_model=AgentLifecycleResponse,
    summary="Reject pending agent",
    responses=_NOT_FOUND,
)
async def reject_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    reason: Annotated[str | None, Query(description="Reason for rejection")] = None,
) -> AgentLifecycleResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    await AgentCoreService.reject_agent(db, agent_id, reason)
    return AgentLifecycleResponse(
        message="Agent rejected",
        agent_name=agent_name,
        status=AgentApprovalStatus.REJECTED,
    )


@router.post(
    "/{agent_name}/pause",
    response_model=AgentLifecycleResponse,
    summary="Pause active agent",
    responses=_NOT_FOUND,
)
async def pause_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    reason: Annotated[str | None, Query(description="Reason for pausing")] = None,
) -> AgentLifecycleResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    await AgentCoreService.pause_agent(db, agent_id, reason)
    return AgentLifecycleResponse(
        message="Agent paused", agent_name=agent_name, status=AgentApprovalStatus.PAUSED
    )


@router.post(
    "/{agent_name}/resume",
    response_model=AgentLifecycleResponse,
    summary="Resume paused agent",
    responses=_NOT_FOUND,
)
async def resume_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentLifecycleResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    await AgentCoreService.resume_agent(db, agent_id)
    return AgentLifecycleResponse(
        message="Agent resumed",
        agent_name=agent_name,
        status=AgentApprovalStatus.ACTIVE,
    )


@router.post(
    "/{agent_name}/disable",
    response_model=AgentLifecycleResponse,
    summary="Disable agent",
    responses=_NOT_FOUND,
)
async def disable_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    reason: Annotated[str | None, Query(description="Reason for disabling")] = None,
) -> AgentLifecycleResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    await AgentCoreService.disable_agent(db, agent_id, reason)
    return AgentLifecycleResponse(
        message="Agent disabled",
        agent_name=agent_name,
        status=AgentApprovalStatus.DISABLED,
    )


@router.post(
    "/{agent_name}/enable",
    response_model=AgentLifecycleResponse,
    summary="Re-enable disabled agent",
    responses=_NOT_FOUND,
)
async def enable_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentLifecycleResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    await AgentCoreService.enable_agent(db, agent_id)
    return AgentLifecycleResponse(
        message="Agent enabled",
        agent_name=agent_name,
        status=AgentApprovalStatus.ACTIVE,
    )


@router.post(
    "/{agent_name}/regenerate-key",
    response_model=AgentKeyIssuedResponse,
    summary="Regenerate agent API key (by name)",
    responses=_NOT_FOUND,
)
async def regenerate_agent_key_by_name(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentKeyIssuedResponse:
    agent_id = await AgentCoreService.agent_id_by_name(db, agent_name)
    api_key = await AgentCoreService.regenerate_active_agent_key(db, agent_id)
    return AgentKeyIssuedResponse(
        message="API key regenerated successfully",
        agent_name=agent_name,
        api_key=api_key,
        status=AgentApprovalStatus.ACTIVE,
    )


@router.get(
    "/{agent_id}/stats",
    response_model=AgentStatsResponse,
    summary="Get agent statistics",
    responses=_NOT_FOUND,
)
async def get_agent_stats(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    hours: Annotated[
        int, Query(ge=1, le=720, description="Hours to include in stats")
    ] = 24,
) -> AgentStatsResponse:
    return await AgentCoreService.get_agent_stats(db, agent_id, hours)


@agent_ops_router.post(
    "/heartbeat",
    response_model=AgentHeartbeatResponse,
    summary="Agent heartbeat",
)
@limiter.limit(settings.security.agent_api_rate_limit)
async def agent_heartbeat(
    request: Request,
    heartbeat: AgentHeartbeat,
    sender: AuthenticatedHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentHeartbeatResponse:
    """Process agent heartbeat. Auth accepts agent-specific key OR registration token."""
    return await AgentCoreService.process_heartbeat(
        db, sender.agent_id, heartbeat, using_agent_key=sender.using_agent_key
    )


# ========== Agent API Key Management Endpoints ==========


@agent_ops_router.post(
    "/agents/{agent_id}/recover-key",
    summary="Recover agent API key",
    description="Agent calls this with registration token to get new agent-specific key",
    response_model=AgentKeyRecoveryResponse,
)
@limiter.limit(settings.security.registration_rate_limit)
async def recover_agent_key(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> AgentKeyRecoveryResponse:
    await verify_registration_token(authorization, db)

    new_key = await AgentCoreService.recover_agent_key(db, agent_id)
    logger.info(
        "Agent recovered API key using registration token",
        extra={"agent_id": str(agent_id)},
    )

    return AgentKeyRecoveryResponse(
        agent_id=agent_id,
        api_key=new_key,
        message="Agent key recovered successfully. Save this key securely - it cannot be retrieved again.",
    )


@agent_ops_router.get(
    "/agents/{agent_id}/api-key",
    summary="Get agent API key",
    description="Agent retrieves its agent-specific key after approval",
)
@limiter.limit(settings.security.registration_rate_limit)
async def get_agent_api_key(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    await verify_registration_token(authorization, db)

    agent = await AgentCoreService.get_agent_by_id(db, agent_id)
    if not agent:
        raise AgentNotFoundException(str(agent_id))

    if agent.approval_status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent not approved yet. Current status: {agent.approval_status}",
        )

    if not agent.api_key_hash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent has no API key. This is an internal error - keys should be auto-generated on approval.",
        )

    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="API key was already issued and cannot be retrieved. Use /recover-key endpoint if key was lost.",
    )


@router.post(
    "/{agent_id}/regenerate-key",
    summary="Regenerate agent API key",
    description="Admin regenerates agent-specific key (revokes old key)",
    response_model=AgentKeyRegenerateResponse,
)
async def regenerate_agent_key(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentKeyRegenerateResponse:
    agent, new_key = await AgentCoreService.regenerate_agent_key(db, agent_id)
    logger.info(
        "Admin regenerated API key for agent",
        extra={"agent_id": str(agent_id)},
    )
    return AgentKeyRegenerateResponse(
        agent_id=agent.id,
        api_key=new_key,
        message="Key regenerated successfully. Update agent configuration with new key.",
    )


@router.delete(
    "/{agent_id}/revoke-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke agent API key",
    description="Admin revokes agent-specific key (blocks agent access)",
)
async def revoke_agent_key(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> None:
    await AgentCoreService.revoke_agent_key(db, agent_id)
    logger.warning(
        "Admin revoked API key for agent",
        extra={"agent_id": str(agent_id)},
    )
