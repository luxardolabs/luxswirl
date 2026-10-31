"""
Agent router - HTTP endpoints for agent operations.

All business logic lives in AgentCoreService. This module only handles HTTP
concerns: request/response, status codes, auth dependencies. The global
LuxSwirlException handler in main.py converts AgentNotFoundException → 404,
so routes do not catch it explicitly.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AuthenticatedHeartbeat,
    KeyedRegistration,
    RecoveryRegistrationKeyRequired,
)
from app.db import get_db
from app.models.enum_model import AgentApprovalStatus
from app.schemas.agent_schema import (
    AgentHeartbeatResponse,
    AgentRegisterResponse,
)
from app.schemas.registration_key_schema import AgentKeyRecoveryResponse
from app.services.core.agent_core_service import AgentCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.agent_router")

# Agent-facing router (agents call these endpoints)
agent_ops_router = APIRouter(tags=["Agent Operations"])


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@agent_ops_router.post(
    "/register",
    response_model=AgentRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new agent",
    description="Register a new agent for approval using registration key. Returns agent UUID to store locally.",
    name="api.agent.registration.create",
)
async def register_agent(
    data: KeyedRegistration,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentRegisterResponse:
    agent, recovery_secret = await AgentCoreService.register_agent(
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
        recovery_secret=recovery_secret,
    )


@agent_ops_router.post(
    "/heartbeat",
    response_model=AgentHeartbeatResponse,
    summary="Agent heartbeat",
    name="api.agent.heartbeat.create",
)
async def agent_heartbeat(
    sender: AuthenticatedHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentHeartbeatResponse:
    """Process agent heartbeat (the body is an `AgentHeartbeat`).

    The agent's own key applies it. The registration key applies it only for an
    agent with no key yet; for one that has a key it only returns the status.
    """
    return await AgentCoreService.process_heartbeat(
        db, sender.agent_id, sender.heartbeat, using_agent_key=sender.using_agent_key
    )


@agent_ops_router.post(
    "/{agent_id}/recover-key",
    summary="Recover agent API key",
    description=(
        "Agent calls this with the registration key AND its own recovery secret "
        "(header X-Agent-Recovery-Secret, issued once at registration) to get a "
        "new agent-specific key"
    ),
    response_model=AgentKeyRecoveryResponse,
    name="api.agent.keys.recover",
)
async def recover_agent_key(
    agent_id: UUID,
    _key: RecoveryRegistrationKeyRequired,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    recovery_secret: Annotated[
        str | None, Header(alias="X-Agent-Recovery-Secret")
    ] = None,
) -> AgentKeyRecoveryResponse:
    new_key = await AgentCoreService.recover_agent_key(db, agent_id, recovery_secret)

    return AgentKeyRecoveryResponse(
        agent_id=agent_id,
        api_key=new_key,
        message="Agent key recovered successfully. Save this key securely - it cannot be retrieved again.",
    )
