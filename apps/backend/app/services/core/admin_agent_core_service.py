"""
Admin agent core service - agents as the admin API serves them.

Each method takes the caller's role; the rules are AgentCoreService's, the
same the web UI keeps: a rename, retag or approval that moves a synthetic
check, and a delete that removes one, are an admin's. A delete runs as a
maintenance job, refused now and judged again when it runs.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import MaintenanceJobKind, UserRole
from app.schemas.admin_agent_schema import (
    AdminAgentReason,
    AdminAgentUpdate,
    AgentKey,
    AgentPage,
)
from app.schemas.agent_schema import AgentResponse
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.schemas.pagination_schema import build_pagination
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

type AgentAction = Literal[
    "approve",
    "reject",
    "pause",
    "resume",
    "disable",
    "enable",
    "archive",
    "restore",
    "force-reload",
]


class AdminAgentCoreService:
    """List, read, change, act on and delete agents, and manage their keys."""

    @staticmethod
    async def list_agents(
        db: AsyncSession,
        *,
        page: int,
        per_page: int,
        search: str | None = None,
        archived: bool = False,
        exclude_pending: bool = False,
    ) -> AgentPage:
        """One page of agents: archived ones only when `archived`."""
        page = max(1, page)
        agents, total = await AgentCoreService.list_agent_responses(
            db,
            search=search,
            offset=(page - 1) * per_page,
            limit=per_page,
            exclude_pending=exclude_pending,
            archived=archived,
        )
        return AgentPage.of(
            agents, build_pagination(page=page, per_page=per_page, total=total)
        )

    @staticmethod
    async def get_agent(db: AsyncSession, agent_id: UUID) -> AgentResponse:
        return await AgentCoreService.get_agent(db, agent_id)

    @staticmethod
    async def update_agent(
        db: AsyncSession, role: UserRole, agent_id: UUID, data: AdminAgentUpdate
    ) -> AgentResponse:
        await AgentCoreService.update_agent(
            db, agent_id, data, actor_is_admin=role == UserRole.ADMIN
        )
        return await AgentCoreService.get_agent(db, agent_id)

    @staticmethod
    async def act(
        db: AsyncSession,
        role: UserRole,
        agent_id: UUID,
        action: AgentAction,
        data: AdminAgentReason | None = None,
    ) -> AgentResponse:
        """Move the agent through its lifecycle; a wrong-state action is refused."""
        reason = data.reason if data is not None else None
        match action:
            case "approve":
                # The key it generates is fetched by the agent with its own
                # recovery secret, as after a web approval: never handed out here.
                await AgentCoreService.approve_agent(
                    db, agent_id, actor_is_admin=role == UserRole.ADMIN
                )
            case "reject":
                await AgentCoreService.reject_agent(db, agent_id, reason)
            case "pause":
                await AgentCoreService.pause_agent(db, agent_id, reason)
            case "resume":
                await AgentCoreService.resume_agent(db, agent_id)
            case "disable":
                await AgentCoreService.disable_agent(db, agent_id, reason)
            case "enable":
                await AgentCoreService.enable_agent(db, agent_id)
            case "archive":
                await AgentCoreService.archive_agent(db, agent_id)
            case "restore":
                await AgentCoreService.restore_agent(db, agent_id)
            case "force-reload":
                await AgentCoreService.force_reload(db, agent_id)
        return await AgentCoreService.get_agent(db, agent_id)

    @staticmethod
    async def enqueue_delete(
        db: AsyncSession, role: UserRole, owner_id: UUID, agent_id: UUID
    ) -> MaintenanceJobResponse:
        """Delete the agent, its checks and their results in the worker."""
        await AgentCoreService.get_agent_by_id(db, agent_id)
        await AgentCoreService.require_may_delete(
            db, agent_id, actor_is_admin=role == UserRole.ADMIN
        )
        return await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.AGENT_DELETE,
            target_id=agent_id,
            owner_id=owner_id,
        )

    @staticmethod
    async def generate_key(db: AsyncSession, agent_id: UUID) -> AgentKey:
        """A first key for an agent with none."""
        agent, api_key = await AgentCoreService.generate_agent_key(db, agent_id)
        return AgentKey(agent_id=agent.id, api_key=api_key)

    @staticmethod
    async def regenerate_key(db: AsyncSession, agent_id: UUID) -> AgentKey:
        """A new key in place of the agent's current one, which stops working."""
        agent, api_key = await AgentCoreService.regenerate_agent_key(db, agent_id)
        return AgentKey(agent_id=agent.id, api_key=api_key)

    @staticmethod
    async def revoke_key(db: AsyncSession, agent_id: UUID) -> None:
        """Clear the agent's key: it cannot connect until it has another."""
        await AgentCoreService.revoke_agent_key(db, agent_id)
