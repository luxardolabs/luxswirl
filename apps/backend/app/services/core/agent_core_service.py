"""
Agent service - business logic for agent operations.
"""

import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import bcrypt
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    AgentNotFoundException,
    DuplicateResourceException,
    ValidationException,
)
from app.crud.agent_crud import AgentCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.models.agent_metric_model import AgentMetric
from app.models.agent_model import Agent
from app.models.enum_model import AgentApprovalStatus
from app.schemas.agent_schema import (
    AgentCreate,
    AgentHeartbeat,
    AgentHeartbeatResponse,
    AgentListResponse,
    AgentResponse,
    AgentStatsResponse,
    AgentUpdate,
)
from app.services.core.job_core_service import JobCoreService
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService

logger = get_logger("luxswirl.services.agent")


class AgentCoreService:
    """Service for agent operations."""

    @staticmethod
    def to_response(agent: Agent) -> AgentResponse:
        """
        Convert Agent model to AgentResponse schema.

        This helper method encapsulates the logic for building an AgentResponse
        from an Agent model, including computed fields like check_count.

        Args:
            agent: Agent model instance

        Returns:
            AgentResponse schema
        """
        check_count = len(agent.checks) if agent.checks else 0

        return AgentResponse(
            id=agent.id,
            agent_name=agent.agent_name,
            agent_run_id=agent.agent_run_id,
            hostname=agent.hostname,
            ip_address=agent.ip_address,
            version=agent.version,
            tags=agent.tags,
            first_seen=agent.first_seen,
            last_seen=agent.last_seen,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
            is_online=agent.is_online,
            uptime_seconds=agent.uptime_seconds,
            check_count=check_count,
        )

    @staticmethod
    async def get_agent_by_id(db: AsyncSession, agent_id: UUID) -> Agent:
        """
        Get agent by UUID.

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Agent instance

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCRUD.get_by_id_with_checks(db, agent_id)

        if not agent:
            raise AgentNotFoundException(str(agent_id))

        return agent

    @staticmethod
    async def resolve_for_assignment(
        db: AsyncSession, agent_id: UUID | None, assignment_mode: str
    ) -> tuple[Agent, UUID]:
        """
        Resolve which agent to use for a check, based on assignment mode.

        Encapsulates the assignment-mode business rule:
        - manual: use the specific agent identified by agent_id
        - replicate / distribute: use the first available active agent as
          a placeholder (the check is not really "owned" by any one agent
          and gets fanned out at scheduling time)

        Args:
            db: Database session
            agent_id: Agent UUID string (used in manual mode)
            assignment_mode: "manual", "replicate", or "distribute"

        Returns:
            Tuple of (Agent, agent_id_string_to_persist)

        Raises:
            AgentNotFoundException: If agent not found, or no agents available
                for replicate/distribute modes.
        """
        if assignment_mode == "manual":
            if agent_id is None:
                raise AgentNotFoundException("Agent required for manual assignment")
            agent = await AgentCoreService.get_agent_by_id(db, agent_id)
            if not agent:
                raise AgentNotFoundException(str(agent_id))
            return agent, agent.id

        # replicate / distribute: pick first available agent as placeholder
        agents, _ = await AgentCoreService.list_agents(db, limit=1, exclude_pending=True)
        if not agents:
            raise AgentNotFoundException("No agents available")
        agent = agents[0]
        return agent, agent.id

    @staticmethod
    async def get_agent_by_name(db: AsyncSession, agent_name: str) -> Agent | None:
        """
        Get agent by agent name.

        Args:
            db: Database session
            agent_name: Agent name string

        Returns:
            Agent instance or None if not found
        """
        return await AgentCRUD.get_by_name_with_checks(db, agent_name)

    @staticmethod
    async def get_agent_by_name_or_raise(db: AsyncSession, agent_name: str) -> Agent:
        """Get agent by name, raising AgentNotFoundException if missing."""
        agent = await AgentCRUD.get_by_name_with_checks(db, agent_name)
        if not agent:
            raise AgentNotFoundException(agent_name)
        return agent

    @staticmethod
    async def create_agent(db: AsyncSession, data: AgentCreate) -> Agent:
        """
        Create a new agent.

        Args:
            db: Database session
            data: Agent creation data

        Returns:
            Created agent

        Raises:
            DuplicateResourceException: If agent_name already exists
        """
        # Explicit creation requires a name (registration sets it later on approval).
        if data.agent_name is None:
            raise ValidationException("agent_name is required to create an agent")

        # Check if agent already exists
        existing = await AgentCoreService.get_agent_by_name(db, data.agent_name)
        if existing:
            raise DuplicateResourceException(f"Agent with name '{data.agent_name}' already exists")

        # Create new agent
        agent = Agent(
            agent_name=data.agent_name,
            agent_run_id=data.agent_run_id,
            hostname=data.hostname,
            ip_address=data.ip_address,
            version=data.version,
            tags=data.tags,
            first_seen=utc_now(),
            last_seen=utc_now(),
        )

        db.add(agent)
        await db.flush()
        await db.refresh(agent)

        logger.info(
            "Created agent",
            extra={"agent_name": agent.agent_name, "agent_id": str(agent.id)},
        )
        return agent

    @staticmethod
    async def register_agent(
        db: AsyncSession,
        hostname: str,
        ip_address: str | None = None,
        version: str | None = None,
        tags: list[str] | None = None,
    ) -> Agent:
        """
        Register a new agent with pending approval status.

        This is used during agent self-registration process. Agent is created
        with approval_status='pending' and no agent_name. Admin must approve
        the agent before it can operate.

        Args:
            db: Database session
            hostname: Agent hostname
            ip_address: Agent IP address
            version: Agent version
            tags: Agent tags

        Returns:
            Created agent with pending status
        """
        agent = Agent(
            agent_name=None,  # Will be set during approval
            hostname=hostname,
            ip_address=ip_address,
            version=version,
            tags=tags or None,
            approval_status="pending",
            first_seen=utc_now(),
            last_seen=utc_now(),
        )

        db.add(agent)
        await db.flush()
        await db.refresh(agent)

        logger.info(
            "Agent registered",
            extra={
                "agent_id": str(agent.id),
                "hostname": hostname,
                "ip_address": ip_address,
            },
        )
        return agent

    @staticmethod
    async def update_agent(db: AsyncSession, agent_id: UUID, data: AgentUpdate) -> Agent:
        """
        Update an agent.

        Args:
            db: Database session
            agent_id: Agent UUID
            data: Update data

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # Update fields
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(agent, field, value)

        agent.updated_at = utc_now()

        await db.flush()
        await db.refresh(agent)

        logger.info(
            "Updated agent",
            extra={"agent_name": agent.agent_name, "agent_id": str(agent.id)},
        )
        return agent

    @staticmethod
    async def update_agent_last_seen(
        db: AsyncSession, agent_id: UUID, agent_run_id: str | None = None
    ) -> Agent:
        """
        Update agent's last_seen timestamp.

        Args:
            db: Database session
            agent_id: Agent UUID
            agent_run_id: Optional run ID

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent doesn't exist
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        if not agent:
            # Agents must register first to get a UUID - no auto-creation
            raise AgentNotFoundException(str(agent_id))
        else:
            # Update existing agent
            agent.last_seen = utc_now()
            if agent_run_id:
                agent.agent_run_id = agent_run_id

        await db.flush()
        await db.refresh(agent)

        return agent

    @staticmethod
    async def list_agents(
        db: AsyncSession,
        active_only: bool = False,
        active_window_minutes: int = 10,
        search: str | None = None,
        offset: int = 0,
        limit: int = 100,
        exclude_pending: bool = False,
    ) -> tuple[Sequence[Agent], int]:
        """
        List agents with pagination.

        Args:
            db: Database session
            active_only: Only return active agents
            active_window_minutes: Minutes to consider agent active
            search: Search in agent_name, hostname, or ip_address (case-insensitive)
            offset: Pagination offset
            limit: Pagination limit
            exclude_pending: Exclude agents with pending/rejected approval status

        Returns:
            Tuple of (agents list, total count)
        """
        return await AgentCRUD.list_filtered_paginated(
            db,
            active_only=active_only,
            active_window_minutes=active_window_minutes,
            search=search,
            exclude_pending=exclude_pending,
            offset=offset,
            limit=limit,
        )

    @staticmethod
    async def list_agents_with_stats(
        db: AsyncSession,
        active_only: bool = False,
        active_window_minutes: int = 10,
        offset: int = 0,
        limit: int = 100,
    ) -> AgentListResponse:
        """
        List agents with aggregated statistics.

        Returns a fully-formed AgentListResponse with online/offline counts
        and per-agent statistics. This method handles all data aggregation
        and transformation, allowing routers to simply return the result.

        Args:
            db: Database session
            active_only: Only return active agents
            active_window_minutes: Minutes to consider agent active
            offset: Pagination offset
            limit: Pagination limit

        Returns:
            AgentListResponse with agents and aggregated stats
        """
        # Get agents from existing list_agents method
        agents, total = await AgentCoreService.list_agents(
            db=db,
            active_only=active_only,
            active_window_minutes=active_window_minutes,
            offset=offset,
            limit=limit,
        )

        # Aggregate statistics
        agent_responses = []
        online_count = 0
        offline_count = 0

        for agent in agents:
            if agent.is_online:
                online_count += 1
            else:
                offline_count += 1

            # Use helper method to build response
            agent_responses.append(AgentCoreService.to_response(agent))

        return AgentListResponse(
            agents=agent_responses,
            total=total,
            online_count=online_count,
            offline_count=offline_count,
        )

    @staticmethod
    async def get_pending_count(db: AsyncSession) -> int:
        """
        Get count of agents with pending approval status.

        Args:
            db: Database session

        Returns:
            Count of pending agents
        """
        return await AgentCRUD.count_pending(db)

    @staticmethod
    async def get_agent_stats(
        db: AsyncSession, agent_id: UUID, hours: int = 24
    ) -> AgentStatsResponse:
        """
        Get agent statistics.

        Args:
            db: Database session
            agent_id: Agent UUID
            hours: Hours to look back for stats

        Returns:
            Agent statistics

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))

        cutoff_time = utc_now() - timedelta(hours=hours)

        row = await CheckResultCRUD.get_stats_for_agent(db, agent.id, cutoff_time)

        total_checks = row.total_checks or 0
        successful_checks = int(row.successful_checks or 0)
        failed_checks = total_checks - successful_checks
        success_rate = (successful_checks / total_checks * 100) if total_checks > 0 else 0.0

        uptime_seconds = (utc_now() - agent.first_seen).total_seconds() if agent.first_seen else 0.0

        return AgentStatsResponse(
            agent_name=agent.agent_name,
            total_checks=total_checks,
            successful_checks=successful_checks,
            failed_checks=failed_checks,
            success_rate=round(success_rate, 2),
            avg_latency_ms=round(row.avg_latency_ms, 2) if row.avg_latency_ms else None,
            uptime_seconds=uptime_seconds,
            last_check_time=row.last_check_time,
        )

    @staticmethod
    async def update_from_heartbeat(
        db: AsyncSession, agent_id: UUID, heartbeat: AgentHeartbeat
    ) -> Agent:
        """
        Update agent from heartbeat data and record metrics.

        Args:
            db: Database session
            agent_id: Agent UUID
            heartbeat: Heartbeat payload

        Returns:
            Updated agent
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        if not agent:
            raise AgentNotFoundException(str(agent_id))

        # Update last_seen
        agent.last_seen = heartbeat.timestamp

        # Update agent fields from heartbeat
        if heartbeat.hostname:
            agent.hostname = heartbeat.hostname
        if heartbeat.ip_address:
            agent.ip_address = heartbeat.ip_address
        if heartbeat.version:
            agent.version = heartbeat.version
        if heartbeat.tags:
            agent.tags = heartbeat.tags

        # Update health fields
        agent.status = heartbeat.status
        agent.uptime_seconds = heartbeat.uptime_seconds
        agent.checks_total = heartbeat.checks_total
        agent.checks_active = heartbeat.checks_active
        agent.checks_executed_total = heartbeat.checks_executed_count
        agent.checks_succeeded_total = heartbeat.checks_succeeded_count
        agent.checks_failed_total = heartbeat.checks_failed_count
        agent.cpu_percent = heartbeat.cpu_percent
        agent.memory_mb = heartbeat.memory_mb
        agent.queue_depth = heartbeat.queue_depth
        agent.last_error = heartbeat.last_error_message
        agent.server_unreachable_count = heartbeat.server_unreachable_count
        agent.stored_reports_count = heartbeat.stored_reports_count
        agent.stored_reports_oldest_timestamp = heartbeat.stored_reports_oldest_timestamp

        # Update resource monitoring fields (SWIRL-57)
        agent.open_file_descriptors = heartbeat.open_file_descriptors
        agent.fd_limit_soft = heartbeat.fd_limit_soft
        agent.fd_usage_percent = heartbeat.fd_usage_percent
        agent.subprocess_count = heartbeat.subprocess_count

        await db.flush()

        # Create AgentMetric record for time-series storage
        metric = AgentMetric(
            agent_id=agent.id,
            timestamp=heartbeat.timestamp,
            cpu_percent=heartbeat.cpu_percent,
            memory_mb=heartbeat.memory_mb,
            queue_depth=heartbeat.queue_depth,
            queue_max_size=heartbeat.queue_max_size,
            checks_executed=heartbeat.checks_executed_count,
            checks_succeeded=heartbeat.checks_succeeded_count,
            checks_failed=heartbeat.checks_failed_count,
            status=heartbeat.status,
            errors_count=heartbeat.errors_since_last_heartbeat,
            warnings_count=heartbeat.warnings_since_last_heartbeat,
            last_error=heartbeat.last_error_message,
            # Resource monitoring (SWIRL-57)
            open_file_descriptors=heartbeat.open_file_descriptors,
            fd_limit_soft=heartbeat.fd_limit_soft,
            fd_usage_percent=heartbeat.fd_usage_percent,
            subprocess_count=heartbeat.subprocess_count,
        )
        db.add(metric)
        await db.flush()

        # Update Prometheus metrics in-memory (instant update on ingestion)

        try:
            MetricsCollectorCoreService.update_agent_status(agent)
            MetricsCollectorCoreService.update_agent_metrics(metric, agent)
        except Exception:
            logger.error("Error updating Prometheus metrics", exc_info=True)

        logger.info(
            "Updated agent from heartbeat",
            extra={
                "agent_name": agent.agent_name,
                "agent_id": str(agent.id),
                "status": heartbeat.status,
                "checks_total": heartbeat.checks_total,
            },
        )

        return agent

    @staticmethod
    async def process_heartbeat(
        db: AsyncSession,
        agent: Agent,
        heartbeat: AgentHeartbeat,
        *,
        using_agent_key: bool,
    ) -> AgentHeartbeatResponse:
        """Apply heartbeat update and build the response payload.

        Caller handles HTTP-layer auth and passes ``using_agent_key`` to gate
        job dispatch. This method handles persistence, gating, and response
        construction.
        """
        agent = await AgentCoreService.update_from_heartbeat(db, agent.id, heartbeat)

        config_version = agent.checks_updated_at.isoformat() if agent.checks_updated_at else None
        heartbeat_interval = agent.heartbeat_interval or settings.server.default_heartbeat_interval
        check_sync_interval = (
            agent.check_sync_interval or settings.server.default_check_sync_interval
        )

        jobs_list: list = []
        message: str | None = None
        if using_agent_key:
            jobs_list = await JobCoreService.get_jobs_for_dispatch(db, agent.id)
        elif agent.approval_status == "active" and agent.api_key_hash:
            message = (
                "Agent approved - retrieve your agent-specific key via "
                "/recover-key endpoint to receive jobs"
            )

        return AgentHeartbeatResponse(
            status="ok",
            config_version=config_version,
            heartbeat_interval=heartbeat_interval,
            check_sync_interval=check_sync_interval,
            message=message,
            jobs=jobs_list,
            approval_status=agent.approval_status,
            report_interval=agent.report_interval,
            report_batch_size=agent.report_batch_size,
            report_max_files_per_batch=agent.report_max_files_per_batch,
            report_process_interval=agent.report_process_interval,
            report_max_queue_size=agent.report_max_queue_size,
            report_backpressure_threshold=agent.report_backpressure_threshold,
            max_concurrent_checks=agent.max_concurrent_checks,
            watchdog_interval=agent.watchdog_interval,
            watchdog_stall_threshold=agent.watchdog_stall_threshold,
            log_level=agent.log_level,
        )

    @staticmethod
    async def delete_agent(db: AsyncSession, agent_id: UUID) -> None:
        """
        Delete an agent (and cascade delete checks and results).

        Args:
            db: Database session
            agent_id: Agent UUID

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        await db.delete(agent)
        await db.flush()

        logger.info(
            "Deleted agent",
            extra={"agent_name": agent.agent_name, "agent_id": str(agent_id)},
        )

    @staticmethod
    async def generate_agent_key(db: AsyncSession, agent_id: UUID) -> tuple[Agent, str]:
        """
        Generate initial API key for agent (if none exists).

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Tuple of (agent, plaintext_key)

        Raises:
            AgentNotFoundException: If agent not found
            ValueError: If agent already has a key
        """

        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        # Check if agent already has a key
        if agent.api_key_hash:
            raise ValueError("Agent already has an API key. Use regenerate instead.")

        # Generate new key
        new_key = f"luxswirl_ak_{secrets.token_hex(16)}"
        salt = bcrypt.gensalt()
        key_hash = bcrypt.hashpw(new_key.encode("utf-8"), salt).decode("utf-8")

        agent.api_key_hash = key_hash
        agent.api_key_created_at = utc_now()

        await db.flush()
        logger.info(
            "Generated initial API key for agent",
            extra={"agent_id": str(agent_id)},
        )

        return agent, new_key

    @staticmethod
    async def regenerate_agent_key(db: AsyncSession, agent_id: UUID) -> tuple[Agent, str]:
        """
        Regenerate API key for agent (revokes old key).

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Tuple of (agent, plaintext_key)

        Raises:
            AgentNotFoundException: If agent not found
        """

        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        # Generate new key
        new_key = f"luxswirl_ak_{secrets.token_hex(16)}"
        salt = bcrypt.gensalt()
        key_hash = bcrypt.hashpw(new_key.encode("utf-8"), salt).decode("utf-8")

        # Revoke old key and set new one
        agent.api_key_hash = key_hash
        agent.api_key_created_at = utc_now()
        agent.api_key_last_used = None  # Reset last used

        await db.flush()
        logger.info(
            "Regenerated API key for agent",
            extra={"agent_id": str(agent_id)},
        )

        return agent, new_key

    @staticmethod
    async def revoke_agent_key(db: AsyncSession, agent_id: UUID) -> Agent:
        """
        Revoke agent API key (clears api_key_hash).

        Agent will be unable to connect until new key is generated or recovered.

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        # Clear key
        agent.api_key_hash = None
        agent.api_key_created_at = None
        agent.api_key_last_used = None

        await db.flush()
        logger.info(
            "Revoked API key for agent",
            extra={"agent_id": str(agent_id)},
        )

        return agent

    # ====================================================================
    # State transition methods
    # ====================================================================

    @staticmethod
    async def approve_agent(db: AsyncSession, agent_id: UUID) -> tuple[Agent, str]:
        """
        Approve a pending agent and generate its API key.

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Tuple of (agent, plaintext_api_key)

        Raises:
            AgentNotFoundException: If agent not found
            ValueError: If agent is already active
        """

        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        if agent.approval_status == "active":
            raise ValueError("Agent is already approved")

        # Generate agent-specific API key
        api_key = f"luxswirl_ak_{secrets.token_hex(16)}"
        salt = bcrypt.gensalt()
        key_hash = bcrypt.hashpw(api_key.encode("utf-8"), salt).decode("utf-8")

        agent.approval_status = AgentApprovalStatus.ACTIVE
        agent.api_key_hash = key_hash
        agent.api_key_created_at = utc_now()
        agent.approved_at = utc_now()
        agent.status_changed_at = utc_now()

        await db.flush()
        logger.info("Approved agent", extra={"agent_id": str(agent_id)})

        return agent, api_key

    @staticmethod
    async def reject_agent(db: AsyncSession, agent_id: UUID, reason: str | None = None) -> Agent:
        """
        Reject a pending agent.

        Args:
            db: Database session
            agent_id: Agent UUID
            reason: Optional rejection reason

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        agent.approval_status = AgentApprovalStatus.REJECTED
        agent.status_reason = reason
        agent.status_changed_at = utc_now()

        await db.flush()
        logger.info("Rejected agent", extra={"agent_id": str(agent_id)})

        return agent

    @staticmethod
    async def pause_agent(db: AsyncSession, agent_id: UUID, reason: str | None = None) -> Agent:
        """
        Pause an active agent.

        Args:
            db: Database session
            agent_id: Agent UUID
            reason: Optional pause reason

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        agent.approval_status = AgentApprovalStatus.PAUSED
        agent.status_reason = reason
        agent.status_changed_at = utc_now()

        await db.flush()
        logger.info("Paused agent", extra={"agent_id": str(agent_id)})

        return agent

    @staticmethod
    async def resume_agent(db: AsyncSession, agent_id: UUID) -> Agent:
        """
        Resume a paused agent.

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        agent.approval_status = AgentApprovalStatus.ACTIVE
        agent.status_reason = None
        agent.status_changed_at = utc_now()

        await db.flush()
        logger.info("Resumed agent", extra={"agent_id": str(agent_id)})

        return agent

    @staticmethod
    async def disable_agent(db: AsyncSession, agent_id: UUID, reason: str | None = None) -> Agent:
        """
        Disable an agent.

        Args:
            db: Database session
            agent_id: Agent UUID
            reason: Optional disable reason

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        agent.approval_status = AgentApprovalStatus.DISABLED
        agent.status_reason = reason
        agent.status_changed_at = utc_now()

        await db.flush()
        logger.info("Disabled agent", extra={"agent_id": str(agent_id)})

        return agent

    @staticmethod
    async def enable_agent(db: AsyncSession, agent_id: UUID) -> Agent:
        """
        Re-enable a disabled agent.

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        agent.approval_status = AgentApprovalStatus.ACTIVE
        agent.status_reason = None
        agent.status_changed_at = utc_now()

        await db.flush()
        logger.info("Enabled agent", extra={"agent_id": str(agent_id)})

        return agent

    @staticmethod
    async def force_reload(db: AsyncSession, agent_id: UUID) -> Agent:
        """
        Force agent config reload by updating checks_updated_at.

        Args:
            db: Database session
            agent_id: Agent UUID

        Returns:
            Updated agent

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)

        agent.checks_updated_at = utc_now()

        await db.flush()
        logger.info(
            "Forced config reload for agent",
            extra={"agent_id": str(agent_id)},
        )

        return agent

    @staticmethod
    async def get_pending_agents(db: AsyncSession) -> Sequence[Agent]:
        """
        Get all agents with pending approval status.

        Args:
            db: Database session

        Returns:
            List of pending agents
        """
        return await AgentCRUD.list_pending(db)

    @staticmethod
    async def get_all_tags(db: AsyncSession) -> list[str]:
        """
        Get all unique tags from all agents.

        Flattens the per-agent tag arrays, trims whitespace, and returns a
        sorted unique list.

        Args:
            db: Database session

        Returns:
            Sorted list of unique tag strings
        """
        agents, _ = await AgentCoreService.list_agents(db, limit=10000, exclude_pending=True)

        tags_set = set()
        for agent in agents:
            for tag in agent.tags or []:
                tag = tag.strip()
                if tag:
                    tags_set.add(tag)

        return sorted(tags_set)

    @staticmethod
    async def get_admitted_agents(db: AsyncSession) -> list[Agent]:
        """
        Get all admitted agents (active, paused, or disabled).

        Excludes pending and rejected. See AgentCRUD.get_admitted_agents for
        why "admitted" rather than "approved".

        Args:
            db: Database session

        Returns:
            List of admitted Agent objects ordered by name
        """
        return await AgentCRUD.get_admitted_agents(db)

    @staticmethod
    async def get_active_agent_count(db: AsyncSession, minutes: int = 10) -> int:
        """
        Get count of agents seen within the given time window.

        Args:
            db: Database session
            minutes: Time window in minutes

        Returns:
            Number of active agents
        """
        return await AgentCRUD.get_active_agent_count(db, minutes)

    @staticmethod
    async def get_agent_metrics(
        db: AsyncSession, agent_id: UUID, cutoff_time: datetime
    ) -> Sequence[AgentMetric]:
        """
        Get agent metrics since a cutoff time, ordered ascending.

        Args:
            db: Database session
            agent_id: Agent UUID
            cutoff_time: Only return metrics after this time

        Returns:
            List of AgentMetric objects ordered by timestamp ascending
        """
        return await AgentCRUD.get_agent_metrics(db, agent_id, cutoff_time)
