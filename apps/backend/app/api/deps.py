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
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AgentNotFoundException
from app.core.security import get_current_user, require_admin, verify_agent_token
from app.db import get_db
from app.models.job_model import Job
from app.schemas.auth_schema import CurrentUserDTO
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.job_core_service import JobCoreService

CurrentUser = Annotated[CurrentUserDTO, Depends(get_current_user)]
AdminUser = Annotated[CurrentUserDTO, Depends(require_admin)]


async def require_job_owner(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Job:
    """The job `job_id`, once its assigned agent has proved its own key.

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
        return job
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
    return job


AgentOwnedJob = Annotated[Job, Depends(require_job_owner)]
