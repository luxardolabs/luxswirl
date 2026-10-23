"""
Agent router - HTTP endpoints for agent operations.

All business logic lives in AgentCoreService. This module only handles HTTP
concerns: request/response, status codes, auth dependencies. The global
LuxSwirlException handler in main.py converts AgentNotFoundException → 404,
so routes do not catch it explicitly.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthenticatedHeartbeat
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import verify_registration_token
from app.db import get_db
from app.models.enum_model import AgentApprovalStatus
from app.schemas.agent_schema import (
    AgentHeartbeat,
    AgentHeartbeatResponse,
    AgentRegisterRequest,
    AgentRegisterResponse,
)
from app.schemas.registration_key_schema import AgentKeyRecoveryResponse
from app.services.core.agent_core_service import AgentCoreService

logger = get_logger("luxswirl.agent_router")

# Agent-facing router (agents call these endpoints)
agent_ops_router = APIRouter(tags=["Agent Operations"])


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@agent_ops_router.post(
    "/agents/register",
    response_model=AgentRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new agent",
    description="Register a new agent for approval using registration key. Returns agent UUID to store locally.",
    name="register_agent",
)
@limiter.limit(settings.security.registration_rate_limit)
async def register_agent(
    request: Request,
    data: AgentRegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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


@agent_ops_router.post(
    "/heartbeat",
    response_model=AgentHeartbeatResponse,
    summary="Agent heartbeat",
    name="agent_heartbeat",
)
@limiter.limit(settings.security.agent_api_rate_limit)
async def agent_heartbeat(
    request: Request,
    heartbeat: AgentHeartbeat,
    sender: AuthenticatedHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentHeartbeatResponse:
    """Process agent heartbeat. Auth accepts agent-specific key OR registration token."""
    return await AgentCoreService.process_heartbeat(
        db, sender.agent_id, heartbeat, using_agent_key=sender.using_agent_key
    )


@agent_ops_router.post(
    "/agents/{agent_id}/recover-key",
    summary="Recover agent API key",
    description="Agent calls this with registration token to get new agent-specific key",
    response_model=AgentKeyRecoveryResponse,
    name="recover_agent_key",
)
@limiter.limit(settings.security.registration_rate_limit)
async def recover_agent_key(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
