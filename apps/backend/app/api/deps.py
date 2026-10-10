"""Typed FastAPI dependency aliases for the JSON API surface.

Split from the old `app/core/auth_deps.py` by SURFACE, which is the point: the
bare and `_Web` dependencies differ in how they REFUSE, and that difference is
the contract of the surface, not a style choice.

- these (API): raise `HTTPException` 401/403, so a programmatic client gets a
  status it can branch on.
- `app/web/deps` (web): raise a 307 to `/login` or `/`, so a browser lands on a
  page instead of a JSON body in an HTMX swap target.

Keeping both sets in one module is how a web page came to answer a JSON 403 and
an API route came to answer a redirect (LUXSWIRL-247). A router importing from
the wrong module now reads wrong at the import line.

Home per FLEET-LAYOUT-STANDARD §2: `app/api/deps` for API dependency aliases.
"""

import contextlib
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AgentNotFoundException
from app.core.security import (
    authenticate_heartbeat,
    get_current_user,
    require_admin,
    require_api_admin,
    verify_agent_token,
)
from app.db import get_db
from app.schemas.agent_schema import AgentHeartbeat
from app.schemas.auth_schema import CurrentUserDTO
from app.schemas.check_result_schema import AgentReportRequest
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.job_core_service import JobCoreService

CurrentUser = Annotated[CurrentUserDTO, Depends(get_current_user)]
AdminUser = Annotated[CurrentUserDTO, Depends(require_admin)]
# A JSON-API bearer token: on this API every token is an admin credential.
ApiAdmin = Annotated[str, Depends(require_api_admin)]


async def require_job_owner(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """`job_id`, once the job's assigned agent has proved its own key.

    Only the agent the job is assigned to may report on it, with its
    agent-specific key (never the registration key). A job with no agent, or
    whose agent is gone or not yet approved, takes no results from anyone.
    Runs before the route body, so nothing is written for a caller that fails.
    """
    job = await JobCoreService.get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Job not found: {job_id}"
        )
    if not settings.security.auth_enabled:
        return job.id
    agent = None
    if job.agent_id is not None:
        with contextlib.suppress(AgentNotFoundException):
            agent = await AgentCoreService.get_agent_by_id(db, job.agent_id)
    if agent is None or not agent.api_key_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Job results are accepted only from the approved agent the job "
                "is assigned to, with its agent-specific key"
            ),
        )
    await verify_agent_token(agent, authorization)
    return job.id


# The id of a job whose assigned agent authenticated this request.
AgentOwnedJobId = Annotated[UUID, Depends(require_job_owner)]


async def require_agent(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """`agent_id`, once that agent has authenticated with its key and is approved.

    Yields the id, not the row: the route hands it to core, which loads what it
    needs, so no ORM agent crosses the seam.
    """
    try:
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}",
        ) from None
    await verify_agent_token(agent, authorization)
    return agent.id


# The id of the agent that authenticated this request (query parameter agent_id).
AuthenticatedAgentId = Annotated[UUID, Depends(require_agent)]


@dataclass(frozen=True)
class HeartbeatSender:
    """Who sent a heartbeat, once authenticated, and with which key."""

    agent_id: UUID
    using_agent_key: bool


async def require_heartbeat_sender(
    heartbeat: AgentHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> HeartbeatSender:
    """The heartbeat's agent, authenticated by its own key or the registration key.

    Reads the same body the route does (FastAPI parses it once). Yields the id
    and which key was used; core loads the agent itself.
    """
    agent = await AgentCoreService.get_agent_by_id(db, heartbeat.agent_id)
    using_agent_key = await authenticate_heartbeat(agent, authorization, db)
    return HeartbeatSender(agent_id=agent.id, using_agent_key=using_agent_key)


AuthenticatedHeartbeat = Annotated[HeartbeatSender, Depends(require_heartbeat_sender)]


async def require_reporting_agent(
    report: AgentReportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """The report's agent, authenticated by its OWN key.

    Check results are accepted only with the agent-specific key, never the
    registration key, so one agent cannot submit results as another. Runs
    before the route body, so nothing is written for a caller that fails.
    """
    try:
        agent = await AgentCoreService.get_agent_by_id(db, report.agent_id)
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {report.agent_id}",
        ) from None
    if not agent.api_key_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Agent must be approved and have agent-specific key to submit "
                "check results"
            ),
        )
    await verify_agent_token(agent, authorization)
    return agent.id


# The id of the agent whose own key authenticated this report.
ReportingAgentId = Annotated[UUID, Depends(require_reporting_agent)]
