"""
Admin API agent router - agents, addressed by their own id.

Reads take any API token; every change takes an editor's or an admin's, as in
the web UI. What only an admin may do (moving or removing a synthetic check)
the core service enforces.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiEditor, ApiUser, require_scope, require_scopes
from app.core.config import settings
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.db import get_db
from app.schemas.admin_agent_schema import (
    AdminAgentReason,
    AdminAgentUpdate,
    AgentKey,
    AgentPage,
)
from app.schemas.admin_page_schema import MAX_PAGE
from app.schemas.agent_schema import AgentResponse
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.admin_agent_core_service import AdminAgentCoreService

router = APIRouter(
    prefix="/agents",
    tags=["Admin API: Agents"],
    dependencies=[Depends(require_scope("agents"))],
)

LIMIT = settings.security.admin_api_rate_limit


@router.get("", response_model=AgentPage, name="api.admin.agents.index")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_list_agents(
    request: Request,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Annotated[str | None, Query(max_length=255)] = None,
    archived: bool = False,
    exclude_pending: bool = False,
) -> AgentPage:
    """One page of agents: search name, hostname or address; archived ones only
    when `archived`; pending and rejected ones left out with `exclude_pending`."""
    return await AdminAgentCoreService.list_agents(
        db,
        page=page,
        per_page=per_page,
        search=search,
        archived=archived,
        exclude_pending=exclude_pending,
    )


@router.get("/{agent_id}", response_model=AgentResponse, name="api.admin.agents.show")
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_get_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    return await AdminAgentCoreService.get_agent(db, agent_id)


@router.patch(
    "/{agent_id}", response_model=AgentResponse, name="api.admin.agents.update"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_update_agent(
    request: Request,
    agent_id: UUID,
    data: AdminAgentUpdate,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    """Change the agent's name, tags or configuration (the fields sent)."""
    return await AdminAgentCoreService.update_agent(
        db, principal.user.role, agent_id, data
    )


@router.delete(
    "/{agent_id}",
    response_model=MaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    name="api.admin.agents.delete",
    # The agent's checks are deleted with it.
    dependencies=[Depends(require_scopes("checks:write"))],
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_delete_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> MaintenanceJobResponse:
    """Delete the agent, its checks and their results in the background.

    Poll `/api/v1/admin/maintenance-jobs/{id}` for the outcome.
    """
    return await AdminAgentCoreService.enqueue_delete(
        db, principal.user.role, principal.user.id, agent_id
    )


# ── Lifecycle ────────────────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/approve", response_model=AgentResponse, name="api.admin.agents.approve"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_approve_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    """Admit a pending or rejected agent; it fetches its key itself."""
    return await AdminAgentCoreService.act(db, principal.user.role, agent_id, "approve")


@router.post(
    "/{agent_id}/reject", response_model=AgentResponse, name="api.admin.agents.reject"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_reject_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    data: AdminAgentReason | None = None,
) -> AgentResponse:
    return await AdminAgentCoreService.act(
        db, principal.user.role, agent_id, "reject", data
    )


@router.post(
    "/{agent_id}/pause", response_model=AgentResponse, name="api.admin.agents.pause"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_pause_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    data: AdminAgentReason | None = None,
) -> AgentResponse:
    return await AdminAgentCoreService.act(
        db, principal.user.role, agent_id, "pause", data
    )


@router.post(
    "/{agent_id}/resume", response_model=AgentResponse, name="api.admin.agents.resume"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_resume_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    return await AdminAgentCoreService.act(db, principal.user.role, agent_id, "resume")


@router.post(
    "/{agent_id}/disable", response_model=AgentResponse, name="api.admin.agents.disable"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_disable_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    data: AdminAgentReason | None = None,
) -> AgentResponse:
    return await AdminAgentCoreService.act(
        db, principal.user.role, agent_id, "disable", data
    )


@router.post(
    "/{agent_id}/enable", response_model=AgentResponse, name="api.admin.agents.enable"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_enable_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    return await AdminAgentCoreService.act(db, principal.user.role, agent_id, "enable")


@router.post(
    "/{agent_id}/archive", response_model=AgentResponse, name="api.admin.agents.archive"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_archive_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    """Retire the agent: it gets no checks, while it and its history are kept."""
    return await AdminAgentCoreService.act(db, principal.user.role, agent_id, "archive")


@router.post(
    "/{agent_id}/restore", response_model=AgentResponse, name="api.admin.agents.restore"
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_restore_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    """Bring an archived agent back as disabled."""
    return await AdminAgentCoreService.act(db, principal.user.role, agent_id, "restore")


@router.post(
    "/{agent_id}/force-reload",
    response_model=AgentResponse,
    name="api.admin.agents.force_reload",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_force_reload_agent(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentResponse:
    """Make the agent fetch its checks again on its next sync."""
    return await AdminAgentCoreService.act(
        db, principal.user.role, agent_id, "force-reload"
    )


# ── The agent's own key ──────────────────────────────────────────────────────

# An agent key reads every one of the agent's checks, credentials included, and
# reports their results: issuing one takes an admin's scopes on both families.
AGENT_KEY_SCOPES = ("agents:admin", "checks:admin")


@router.post(
    "/{agent_id}/key",
    response_model=AgentKey,
    status_code=status.HTTP_201_CREATED,
    name="api.admin.agent_keys.create",
    dependencies=[Depends(require_scopes(*AGENT_KEY_SCOPES))],
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_generate_agent_key(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentKey:
    """A first key for an agent with none, shown this once."""
    return await AdminAgentCoreService.generate_key(db, agent_id)


@router.post(
    "/{agent_id}/key/regenerate",
    response_model=AgentKey,
    status_code=status.HTTP_201_CREATED,
    name="api.admin.agent_keys.regenerate",
    dependencies=[Depends(require_scopes(*AGENT_KEY_SCOPES))],
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_regenerate_agent_key(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> AgentKey:
    """A new key, shown this once; the old one stops working now."""
    return await AdminAgentCoreService.regenerate_key(db, agent_id)


@router.delete(
    "/{agent_id}/key",
    status_code=status.HTTP_204_NO_CONTENT,
    name="api.admin.agent_keys.delete",
)
@limiter.limit(LIMIT, key_func=api_token_rate_limit_key)
async def admin_revoke_agent_key(
    request: Request,
    agent_id: UUID,
    principal: ApiEditor,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Revoke the agent's key: it cannot connect until it has another."""
    await AdminAgentCoreService.revoke_key(db, agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
