"""Typed FastAPI dependency aliases for the JSON API surface.

Split from the old `app/core/auth_deps.py` by SURFACE, which is the point: the
bare and `_Web` dependencies differ in how they REFUSE, and that difference is
the contract of the surface, not a style choice.

- these (API): raise `HTTPException` 401/403, so a programmatic client gets a
  status it can branch on.
- `app/web/deps` (web): raise a 307 to `/login` or `/`, so a browser lands on a
  page instead of a JSON body in an HTMX swap target.

Keeping both sets in one module lets a web page answer a JSON 403 and an API
route answer a redirect. Apart, a router importing from the wrong module reads
wrong at the import line.

Home per FLEET-LAYOUT-STANDARD §2: `app/api/deps` for API dependency aliases.
"""

import contextlib
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AgentNotFoundException
from app.core.rate_limit import limiter
from app.core.security import (
    authenticate_heartbeat,
    verify_agent_token,
    verify_recovery_registration_key,
    verify_registration_token,
)
from app.db import get_db
from app.models.enum_model import UserRole
from app.schemas.agent_schema import AgentHeartbeat
from app.schemas.api_token_schema import ApiPrincipal
from app.schemas.check_artifact_schema import CheckArtifactCreate
from app.schemas.check_result_schema import AgentReportSubmission
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.job_core_service import JobCoreService

# ── The agent protocol: the agent's own key, or the registration key ─────────
#
# Each dependency that checks a key carries the route's rate limit itself.
# FastAPI resolves dependencies before the handler, so a limit on the handler
# never sees a request refused here: the key could be guessed without limit.
# Limited here, every call counts, the refused ones included.


@limiter.limit(settings.security.registration_rate_limit)
async def require_registration_key(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Passes once the caller has presented a valid registration key."""
    await verify_registration_token(authorization, db)


# A request authenticated by the registration key (agent registration).
RegistrationKeyRequired = Annotated[None, Depends(require_registration_key)]


@limiter.limit(settings.security.registration_rate_limit)
async def require_recovery_registration_key(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Passes once a key recovery for `agent_id` has presented a valid
    registration key; a refusal is audited. The agent's own recovery secret is
    the other half, checked in core."""
    await verify_recovery_registration_key(agent_id, authorization, db)


# A key recovery authenticated by the registration key (path parameter agent_id).
RecoveryRegistrationKeyRequired = Annotated[
    None, Depends(require_recovery_registration_key)
]


@limiter.limit(settings.security.agent_api_rate_limit)
async def require_job_owner(
    request: Request,
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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


@limiter.limit(settings.security.agent_api_rate_limit)
async def require_agent(
    request: Request,
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
    """A heartbeat and who sent it, once authenticated, and with which key."""

    heartbeat: AgentHeartbeat
    agent_id: UUID
    using_agent_key: bool


@limiter.limit(settings.security.agent_api_rate_limit)
async def require_heartbeat_sender(
    request: Request,
    heartbeat: AgentHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> HeartbeatSender:
    """The heartbeat's agent, authenticated by its own key or the registration key.

    The body is declared here only: a second declaration on the route would be
    validated again, and each error reported twice. Yields the parsed heartbeat,
    the id and which key was used; core loads the agent itself.
    """
    agent = await AgentCoreService.get_agent_by_id(db, heartbeat.agent_id)
    using_agent_key = await authenticate_heartbeat(agent, authorization, db)
    return HeartbeatSender(
        heartbeat=heartbeat, agent_id=agent.id, using_agent_key=using_agent_key
    )


AuthenticatedHeartbeat = Annotated[HeartbeatSender, Depends(require_heartbeat_sender)]


@limiter.limit(settings.security.agent_api_rate_limit)
async def require_reporting_agent(
    request: Request,
    report: AgentReportSubmission,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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


@limiter.limit(settings.security.agent_api_rate_limit)
async def require_uploading_agent(
    request: Request,
    artifact: CheckArtifactCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """The artifact's agent, authenticated by its OWN key, exactly as for reports.

    Never the registration key, so one agent cannot upload as another. Proves
    only WHO is uploading; whether the artifact's check is that agent's is the
    assignment rule core applies on create. Runs before the route body, so
    nothing is written for a caller that fails.
    """
    try:
        agent = await AgentCoreService.get_agent_by_id(db, artifact.agent_id)
    except AgentNotFoundException:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {artifact.agent_id}",
        ) from None
    if not agent.api_key_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Agent must be approved and have agent-specific key to upload artifacts"
            ),
        )
    await verify_agent_token(agent, authorization)
    return agent.id


# The id of the agent whose own key authenticated this artifact upload.
UploadingAgentId = Annotated[UUID, Depends(require_uploading_agent)]


# ── The admin API: a user's API token, with that user's role ─────────────────
#
# Every token is 32 random bytes, so a guess is not worth rate-limiting at the
# refusal; each route's limit is per token (`api_token_rate_limit_key`). There
# is no auth-disabled mode here: a request with no token acts as nobody.


async def require_api_user(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    authorization: Annotated[str | None, Header()] = None,
) -> ApiPrincipal:
    """The user whose unrevoked, unexpired API token authenticated this request."""
    scheme, _, token = (authorization or "").partition(" ")
    principal = (
        await ApiTokenCoreService.authenticate(db, token.strip())
        if scheme.lower() == "bearer"
        else None
    )
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid API token is required (Authorization: Bearer lsw_...)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def require_api_editor(
    principal: Annotated[ApiPrincipal, Depends(require_api_user)],
) -> ApiPrincipal:
    """An API token whose user is an editor or an admin."""
    if principal.user.role not in (UserRole.ADMIN, UserRole.EDITOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Editor or Admin access required",
        )
    return principal


async def require_api_admin(
    principal: Annotated[ApiPrincipal, Depends(require_api_user)],
) -> ApiPrincipal:
    """An API token whose user is an admin."""
    if principal.user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    return principal


# Who an admin-API request acts as, by the role its route requires.
ApiUser = Annotated[ApiPrincipal, Depends(require_api_user)]
ApiEditor = Annotated[ApiPrincipal, Depends(require_api_editor)]
ApiAdmin = Annotated[ApiPrincipal, Depends(require_api_admin)]
