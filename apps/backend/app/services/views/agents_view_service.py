"""
Agents service - provides agent list data for web UI.

This web service acts as an aggregation layer, delegating to core services
while providing web-specific functionality and data aggregation.
"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.models.agent_model import Agent
from app.models.enum_model import MaintenanceJobKind
from app.models.user_model import User
from app.schemas.agent_schema import AgentUpdate
from app.schemas.pagination_schema import build_pagination
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.web.services.agents")


class AgentData:
    """Data structure for agent list display."""

    def __init__(
        self,
        agent: Agent,
        total_checks: int,
        enabled_checks: int,
        cpu_sparkline: list[float | None],
        memory_sparkline: list[float | None],
        queue_sparkline: list[int | None],
        checks_executed_sparkline: list[int | None],
        fd_sparkline: list[int | None],
        subprocess_sparkline: list[int | None],
        default_heartbeat: int,
        default_check_sync: int,
    ):
        self.agent = agent
        self.total_checks = total_checks
        self.enabled_checks = enabled_checks
        self.cpu_sparkline = cpu_sparkline
        self.memory_sparkline = memory_sparkline
        self.queue_sparkline = queue_sparkline
        self.checks_executed_sparkline = checks_executed_sparkline
        self.fd_sparkline = fd_sparkline
        self.subprocess_sparkline = subprocess_sparkline
        self.default_heartbeat = default_heartbeat
        self.default_check_sync = default_check_sync


class AgentsViewService:
    """Service for agents page data aggregation."""

    @staticmethod
    async def get_agents_with_check_counts(
        db: AsyncSession,
        active_only: bool = False,
        search: str | None = None,
        hours: int = 4,
        limit: int = 20,
        offset: int = 0,
        exclude_pending: bool = False,
    ) -> tuple[list[AgentData], int]:
        """
        Get agents with their check counts.

        Args:
            db: Database session
            active_only: Only show active agents
            search: Search in agent_name, hostname, or ip_address
            hours: Hours of metrics history to include
            limit: Max results per page
            offset: Pagination offset
            exclude_pending: Exclude agents with pending/rejected approval status

        Returns:
            Tuple of (agent data list, total count)
        """
        # Get agents
        agents, total = await AgentCoreService.list_agents(
            db=db,
            active_only=active_only,
            search=search,
            limit=limit,
            offset=offset,
            exclude_pending=exclude_pending,
        )

        # Get check counts and sparklines for each agent in parallel
        async def get_agent_data(agent: Agent) -> AgentData:
            # Get check counts
            checks = await CheckCoreService.list_checks_for_agent(db, agent.id)
            enabled_checks = sum(1 for c in checks if c.enabled)

            # Get sparkline data with smart bucketing (max 200 samples for performance)
            cutoff_time = utc_now() - timedelta(hours=hours)
            all_metrics = await AgentCoreService.get_agent_metrics(db, agent.id, cutoff_time)

            # Smart bucketing: ~200 samples max, but use appropriate aggregation per metric
            max_samples = 200
            if len(all_metrics) <= max_samples:
                # Few enough points - use all data
                cpu_sparkline = [
                    float(m.cpu_percent) if m.cpu_percent is not None else None for m in all_metrics
                ]
                memory_sparkline = [
                    float(m.memory_mb) if m.memory_mb is not None else None for m in all_metrics
                ]
                queue_sparkline: list[int | None] = [
                    m.queue_depth if m.queue_depth is not None else 0 for m in all_metrics
                ]
                checks_executed_sparkline: list[int | None] = [
                    m.checks_executed or 0 for m in all_metrics
                ]
                fd_sparkline = [
                    m.open_file_descriptors if m.open_file_descriptors is not None else None
                    for m in all_metrics
                ]
                subprocess_sparkline = [
                    m.subprocess_count if m.subprocess_count is not None else None
                    for m in all_metrics
                ]
            else:
                # Too many points - bucket with appropriate aggregation
                bucket_size = len(all_metrics) // max_samples
                cpu_sparkline = []
                memory_sparkline = []
                queue_sparkline = []
                checks_executed_sparkline = []
                fd_sparkline = []
                subprocess_sparkline = []

                for i in range(0, len(all_metrics), bucket_size):
                    bucket = all_metrics[i : i + bucket_size]

                    # CPU: Average (typical usage)
                    cpu_vals = [m.cpu_percent for m in bucket if m.cpu_percent is not None]
                    cpu_sparkline.append(float(sum(cpu_vals) / len(cpu_vals)) if cpu_vals else None)

                    # Memory: Average (typical usage)
                    mem_vals = [m.memory_mb for m in bucket if m.memory_mb is not None]
                    memory_sparkline.append(
                        float(sum(mem_vals) / len(mem_vals)) if mem_vals else None
                    )

                    # Queue Depth: MAX (catch spikes - critical for monitoring!)
                    queue_vals = [m.queue_depth for m in bucket if m.queue_depth is not None]
                    queue_sparkline.append(max(queue_vals) if queue_vals else 0)

                    # Checks Executed: SUM (total in bucket)
                    checks_executed_sparkline.append(sum(m.checks_executed or 0 for m in bucket))

                    # File Descriptors: MAX (catch leaks - critical!)
                    fd_vals = [
                        m.open_file_descriptors
                        for m in bucket
                        if m.open_file_descriptors is not None
                    ]
                    fd_sparkline.append(max(fd_vals) if fd_vals else None)

                    # Subprocesses: MAX (catch leaks - critical!)
                    sub_vals = [
                        m.subprocess_count for m in bucket if m.subprocess_count is not None
                    ]
                    subprocess_sparkline.append(max(sub_vals) if sub_vals else None)

                # Trim to exact max_samples
                cpu_sparkline = cpu_sparkline[:max_samples]
                memory_sparkline = memory_sparkline[:max_samples]
                queue_sparkline = queue_sparkline[:max_samples]
                checks_executed_sparkline = checks_executed_sparkline[:max_samples]
                fd_sparkline = fd_sparkline[:max_samples]
                subprocess_sparkline = subprocess_sparkline[:max_samples]

            return AgentData(
                agent=agent,
                total_checks=len(checks),
                enabled_checks=enabled_checks,
                cpu_sparkline=cpu_sparkline,
                memory_sparkline=memory_sparkline,
                queue_sparkline=queue_sparkline,
                checks_executed_sparkline=checks_executed_sparkline,
                fd_sparkline=fd_sparkline,
                subprocess_sparkline=subprocess_sparkline,
                default_heartbeat=settings.server.default_heartbeat_interval,
                default_check_sync=settings.server.default_check_sync_interval,
            )

        # Execute all queries concurrently
        agent_data = await asyncio.gather(*[get_agent_data(agent) for agent in agents])

        return agent_data, total

    # ====================================================================
    # Web-specific wrapper methods (delegate to core services)
    # ====================================================================

    @staticmethod
    async def get_agent_by_id(db: AsyncSession, agent_id: UUID):
        """Get agent by ID."""
        return await AgentCoreService.get_agent_by_id(db, agent_id)

    @staticmethod
    async def get_all_tags(db: AsyncSession) -> list[str]:
        """Get all unique tags from all agents."""
        return await AgentCoreService.get_all_tags(db)

    @staticmethod
    async def update_agent(db: AsyncSession, agent_id: UUID, update_data: AgentUpdate):
        """Update an agent."""
        return await AgentCoreService.update_agent(db, agent_id, update_data)

    @staticmethod
    async def delete_agent(db: AsyncSession, agent_id: UUID):
        """Sync delete — kept for internal/test callers only.

        Web routes go through enqueue_delete() so the cascade runs in the
        maintenance worker. See LUXSWIRL-105.
        """
        return await AgentCoreService.delete_agent(db, agent_id)

    @staticmethod
    async def enqueue_delete(db: AsyncSession, agent_id: UUID, owner_id: UUID | None = None):
        """Enqueue an agent_delete maintenance job; returns the job row."""
        # 404 cleanly if the agent doesn't exist instead of queuing a doomed job.
        await AgentCoreService.get_agent_by_id(db, agent_id)
        return await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.AGENT_DELETE,
            target_id=agent_id,
            owner_id=owner_id,
        )

    @staticmethod
    async def get_pending_count(db: AsyncSession) -> int:
        """Get count of pending agents."""
        return await AgentCoreService.get_pending_count(db)

    @staticmethod
    async def get_setting(db: AsyncSession, key: str, default):
        """Get a setting value."""
        return await SettingsCoreService.get_setting(db, key, default)

    # ------------------------------------------------------------------
    # Agent lifecycle pass-throughs (so the router stays dumb).
    # ------------------------------------------------------------------

    @staticmethod
    async def get_pending_agents(db: AsyncSession):
        """List pending agents (not paginated)."""
        return await AgentCoreService.get_pending_agents(db)

    @staticmethod
    async def approve_agent(db: AsyncSession, agent_id: UUID):
        """Approve agent + auto-generate API key. Returns (Agent, api_key)."""
        return await AgentCoreService.approve_agent(db, agent_id)

    @staticmethod
    async def reject_agent(db: AsyncSession, agent_id: UUID, reason: str | None):
        """Reject agent. Returns Agent."""
        return await AgentCoreService.reject_agent(db, agent_id, reason)

    @staticmethod
    async def pause_agent(db: AsyncSession, agent_id: UUID, reason: str | None):
        return await AgentCoreService.pause_agent(db, agent_id, reason)

    @staticmethod
    async def resume_agent(db: AsyncSession, agent_id: UUID):
        return await AgentCoreService.resume_agent(db, agent_id)

    @staticmethod
    async def disable_agent(db: AsyncSession, agent_id: UUID, reason: str | None):
        return await AgentCoreService.disable_agent(db, agent_id, reason)

    @staticmethod
    async def enable_agent(db: AsyncSession, agent_id: UUID):
        return await AgentCoreService.enable_agent(db, agent_id)

    @staticmethod
    async def force_reload(db: AsyncSession, agent_id: UUID):
        return await AgentCoreService.force_reload(db, agent_id)

    @staticmethod
    async def generate_agent_key(db: AsyncSession, agent_id: UUID):
        """Generate initial API key. Returns (Agent, plaintext_key). Raises ValueError if key exists."""
        return await AgentCoreService.generate_agent_key(db, agent_id)

    @staticmethod
    async def regenerate_agent_key(db: AsyncSession, agent_id: UUID):
        """Regenerate API key (revokes old). Returns (Agent, plaintext_key)."""
        return await AgentCoreService.regenerate_agent_key(db, agent_id)

    @staticmethod
    async def revoke_agent_key(db: AsyncSession, agent_id: UUID):
        return await AgentCoreService.revoke_agent_key(db, agent_id)

    # ------------------------------------------------------------------
    # Page / partial / form context builders.
    # ------------------------------------------------------------------

    @staticmethod
    async def build_agents_list_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        active_only: bool,
        search: str,
        hours: int,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """Full /agents page context."""
        if per_page is None:
            per_page = await AgentsViewService.get_setting(db, "general.default_page_size", 50)
        agent_stale_threshold = await AgentsViewService.get_setting(
            db, "general.agent_stale_threshold_seconds", 300
        )
        offset = (page - 1) * per_page

        pending_agents = await AgentsViewService.get_pending_agents(db)
        agent_data, total = await AgentsViewService.get_agents_with_check_counts(
            db=db,
            active_only=active_only,
            search=search if search else None,
            hours=hours,
            limit=per_page,
            offset=offset,
            exclude_pending=True,
        )
        pagination = build_pagination(
            page=page,
            per_page=per_page,
            total=total,
            filters={
                "active_only": active_only,
                "search": search,
                "hours": hours,
            },
        )
        return {
            "request": request,
            "current_user": current_user,
            "agent_data": agent_data,
            "pending_agents": pending_agents,
            "active_only": active_only,
            "search": search,
            "hours": hours,
            "pagination": pagination,
            "page_title": "Agents",
            "default_heartbeat": settings.server.default_heartbeat_interval,
            "default_check_sync": settings.server.default_check_sync_interval,
            "now_timestamp": utc_now().timestamp(),
            "agent_stale_threshold": agent_stale_threshold,
        }

    @staticmethod
    async def build_edit_form_context(
        db: AsyncSession, request: Request, current_user: User, agent_id: UUID
    ) -> dict[str, Any] | None:
        """Edit-form panel context. Returns None if agent not found."""
        agent = await AgentsViewService.get_agent_by_id(db, agent_id)
        if not agent:
            return None
        available_agent_tags = await AgentsViewService.get_all_tags(db)
        check_count = len(agent.checks) if agent.checks else 0
        return {
            "request": request,
            "current_user": current_user,
            "agent": agent,
            "check_count": check_count,
            "default_heartbeat": settings.server.default_heartbeat_interval,
            "default_check_sync": settings.server.default_check_sync_interval,
            "available_agent_tags": available_agent_tags,
        }

    @staticmethod
    async def build_key_management_panel_context(
        db: AsyncSession, request: Request, current_user: User, agent_id: UUID
    ) -> dict[str, Any] | None:
        """Key-management-panel context. Returns None if agent not found."""
        agent = await AgentsViewService.get_agent_by_id(db, agent_id)
        if not agent:
            return None
        return {
            "request": request,
            "current_user": current_user,
            "agent": agent,
        }

    @staticmethod
    def build_key_generated_panel_context(
        request: Request, current_user: User, agent: Agent, plaintext_key: str
    ) -> dict[str, Any]:
        """'Key generated — copy this once' panel context."""
        return {
            "request": request,
            "current_user": current_user,
            "agent": agent,
            "plaintext_key": plaintext_key,
        }

    # ------------------------------------------------------------------
    # Mutation orchestrators.
    # ------------------------------------------------------------------

    @staticmethod
    async def handle_update_agent_form(
        db: AsyncSession, agent_id: UUID, form_kwargs: dict[str, Any]
    ) -> tuple[str, str, int]:
        """
        Apply an agent-update form. Returns (kind, message, status):
          kind ∈ {"success", "error"}
        Trims & coerces form fields, then triggers force_reload so the agent
        picks up the new config on the next heartbeat.
        """
        try:
            update_dict: dict[str, Any] = {}

            agent_name = form_kwargs.get("agent_name")
            if agent_name is not None and agent_name.strip():
                update_dict["agent_name"] = agent_name.strip()

            hostname = form_kwargs.get("hostname")
            if hostname is not None and hostname.strip():
                update_dict["hostname"] = hostname.strip()

            tags = form_kwargs.get("tags")
            if tags is not None:
                parsed_tags = [t.strip() for t in str(tags).split(",") if t.strip()]
                update_dict["tags"] = parsed_tags or None

            def _parse_int(v, name):
                if v is not None and str(v).strip():
                    try:
                        n = int(v)
                        if n > 0:
                            update_dict[name] = n
                        elif n == 0:
                            update_dict[name] = None
                    except ValueError:
                        pass

            def _parse_float(v, name):
                if v is not None and str(v).strip():
                    try:
                        f = float(v)
                        if f > 0:
                            update_dict[name] = f
                        elif f == 0:
                            update_dict[name] = None
                    except ValueError:
                        pass

            _parse_int(form_kwargs.get("heartbeat_interval"), "heartbeat_interval")
            _parse_int(form_kwargs.get("check_sync_interval"), "check_sync_interval")
            _parse_int(form_kwargs.get("report_interval"), "report_interval")
            _parse_int(form_kwargs.get("report_batch_size"), "report_batch_size")
            _parse_int(form_kwargs.get("report_max_files_per_batch"), "report_max_files_per_batch")
            _parse_int(form_kwargs.get("report_process_interval"), "report_process_interval")
            _parse_int(form_kwargs.get("report_max_queue_size"), "report_max_queue_size")
            _parse_float(
                form_kwargs.get("report_backpressure_threshold"),
                "report_backpressure_threshold",
            )
            _parse_int(form_kwargs.get("max_concurrent_checks"), "max_concurrent_checks")
            _parse_int(form_kwargs.get("watchdog_interval"), "watchdog_interval")
            _parse_int(form_kwargs.get("watchdog_stall_threshold"), "watchdog_stall_threshold")

            log_level = form_kwargs.get("log_level")
            if log_level is not None:
                ll = str(log_level).strip()
                update_dict["log_level"] = ll if ll else None

            update_data = AgentUpdate(**update_dict)
            await AgentsViewService.update_agent(db, agent_id, update_data)
            await AgentsViewService.force_reload(db, agent_id)
            logger.info(
                "Updated agent via web UI",
                extra={"agent_id": str(agent_id)},
            )
            return "success", "Agent updated successfully!", 200
        except AgentNotFoundException:
            return "error", f"Agent not found: {agent_id}", 404
        except Exception as e:
            logger.error("Error updating agent", exc_info=True)
            return "error", str(e), 500
