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

from app.core.config import settings
from app.core.exceptions import AgentNotFoundException
from app.core.rate_limit import limiter
from app.core.security import (
    verify_agent_token,
    verify_api_token,
    verify_registration_token,
)
from app.db import get_db
from app.models.enum_model import MaintenanceJobKind
from app.schemas.agent_schema import (
    AgentCreate,
    AgentHeartbeat,
    AgentHeartbeatResponse,
    AgentListResponse,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentResponse,
    AgentStatsResponse,
    AgentUpdate,
)
from app.schemas.base import ErrorResponse
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


async def _authenticate_heartbeat(
    agent,
    authorization: str | None,
    db: AsyncSession,
) -> bool:
    """Authenticate a heartbeat. Returns True if agent-specific key was used."""
    if agent.api_key_hash:
        try:
            await verify_agent_token(agent, authorization)
            return True
        except HTTPException as e:
            if e.status_code != 401:
                raise
    await verify_registration_token(authorization, db)
    return False


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
):
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
        status="pending",
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
    active_only: Annotated[bool, Query(description="Only return active agents")] = False,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
):
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
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    return AgentCoreService.to_response(agent)


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
):
    agent = await AgentCoreService.create_agent(db, data)
    return AgentCoreService.to_response(agent)


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
):
    agent = await AgentCoreService.update_agent(db, agent_id, data)
    return AgentCoreService.to_response(agent)


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue agent delete (async — runs in maintenance worker)",
    responses=_NOT_FOUND,
)
async def delete_agent(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
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
    return {"job_id": str(job.id), "status": job.status, "kind": job.kind}


@router.post(
    "/{agent_name}/approve",
    response_model=dict,
    summary="Approve pending agent",
    responses=_NOT_FOUND,
)
async def approve_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    _approved_agent, api_key = await AgentCoreService.approve_agent(db, agent.id)
    return {
        "message": "Agent approved successfully",
        "agent_name": agent_name,
        "api_key": api_key,
        "status": "active",
    }


@router.post(
    "/{agent_name}/reject",
    response_model=dict,
    summary="Reject pending agent",
    responses=_NOT_FOUND,
)
async def reject_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    reason: Annotated[str | None, Query(description="Reason for rejection")] = None,
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    await AgentCoreService.reject_agent(db, agent.id, reason)
    return {"message": "Agent rejected", "agent_name": agent_name, "status": "rejected"}


@router.post(
    "/{agent_name}/pause",
    response_model=dict,
    summary="Pause active agent",
    responses=_NOT_FOUND,
)
async def pause_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    reason: Annotated[str | None, Query(description="Reason for pausing")] = None,
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    await AgentCoreService.pause_agent(db, agent.id, reason)
    return {"message": "Agent paused", "agent_name": agent_name, "status": "paused"}


@router.post(
    "/{agent_name}/resume",
    response_model=dict,
    summary="Resume paused agent",
    responses=_NOT_FOUND,
)
async def resume_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    await AgentCoreService.resume_agent(db, agent.id)
    return {"message": "Agent resumed", "agent_name": agent_name, "status": "active"}


@router.post(
    "/{agent_name}/disable",
    response_model=dict,
    summary="Disable agent",
    responses=_NOT_FOUND,
)
async def disable_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    reason: Annotated[str | None, Query(description="Reason for disabling")] = None,
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    await AgentCoreService.disable_agent(db, agent.id, reason)
    return {"message": "Agent disabled", "agent_name": agent_name, "status": "disabled"}


@router.post(
    "/{agent_name}/enable",
    response_model=dict,
    summary="Re-enable disabled agent",
    responses=_NOT_FOUND,
)
async def enable_agent(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    await AgentCoreService.enable_agent(db, agent.id)
    return {"message": "Agent enabled", "agent_name": agent_name, "status": "active"}


@router.post(
    "/{agent_name}/regenerate-key",
    response_model=dict,
    summary="Regenerate agent API key (by name)",
    responses=_NOT_FOUND,
)
async def regenerate_agent_key_by_name(
    agent_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    agent = await AgentCoreService.get_agent_by_name_or_raise(db, agent_name)
    if agent.approval_status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only regenerate keys for active agents",
        )
    _regen_agent, api_key = await AgentCoreService.regenerate_agent_key(db, agent.id)
    return {
        "message": "API key regenerated successfully",
        "agent_name": agent_name,
        "api_key": api_key,
    }


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
    hours: Annotated[int, Query(ge=1, le=720, description="Hours to include in stats")] = 24,
):
    return await AgentCoreService.get_agent_stats(db, agent_id, hours)


@agent_ops_router.post(
    "/heartbeat",
    response_model=AgentHeartbeatResponse,
    summary="Agent heartbeat",
)
async def agent_heartbeat(
    heartbeat: AgentHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    """Process agent heartbeat. Auth accepts agent-specific key OR registration token."""
    agent = await AgentCoreService.get_agent_by_id(db, heartbeat.agent_id)
    if not agent:
        raise AgentNotFoundException(str(heartbeat.agent_id))

    using_agent_key = await _authenticate_heartbeat(agent, authorization, db)

    try:
        return await AgentCoreService.process_heartbeat(
            db, agent, heartbeat, using_agent_key=using_agent_key
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process heartbeat: {e}",
        ) from e


# ========== Agent API Key Management Endpoints ==========


@agent_ops_router.post(
    "/agents/{agent_id}/recover-key",
    summary="Recover agent API key",
    description="Agent calls this with registration token to get new agent-specific key",
)
async def recover_agent_key(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    await verify_registration_token(authorization, db)

    agent = await AgentCoreService.get_agent_by_id(db, agent_id)
    if not agent:
        raise AgentNotFoundException(str(agent_id))

    if agent.approval_status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent must be active to recover key. Current status: {agent.approval_status}",
        )

    _, new_key = await AgentCoreService.regenerate_agent_key(db, agent_id)
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
async def get_agent_api_key(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
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
)
async def regenerate_agent_key(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
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
):
    await AgentCoreService.revoke_agent_key(db, agent_id)
    logger.warning(
        "Admin revoked API key for agent",
        extra={"agent_id": str(agent_id)},
    )
