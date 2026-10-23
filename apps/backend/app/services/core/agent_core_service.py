"""
Agent service - business logic for agent operations.
"""

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import bcrypt
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    AgentNotFoundException,
    AuthenticationException,
    AuthorizationException,
    DuplicateResourceException,
    ValidationException,
)
from app.crud.agent_crud import AgentCRUD
from app.models.agent_metric_model import AgentMetric
from app.models.agent_model import Agent
from app.models.enum_model import AgentApprovalStatus
from app.schemas.agent_schema import (
    AgentCreate,
    AgentHeartbeat,
    AgentHeartbeatResponse,
    AgentMetricSample,
    AgentOption,
    AgentResponse,
    AgentUpdate,
    PendingAgent,
)
from app.services.core.job_core_service import JobCoreService
from app.services.core.metrics_collector_core_service import MetricsCollectorCoreService

logger = get_logger("luxswirl.services.agent")


def _recovery_secret_digest(secret: str) -> str:
    """The stored form of a recovery secret: its SHA-256 hex digest.

    A fast hash is right here: the secret is 256 random bits, not a password,
    so there is nothing for a slow hash to stretch.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


# Compared against when no digest is on record, so a refusal does the same work.
_NO_SECRET_DIGEST = "0" * 64


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
            approval_status=agent.approval_status,
            api_key_created_at=agent.api_key_created_at,
            # The hash never leaves the core service — only whether one exists.
            has_api_key=bool(agent.api_key_hash),
            heartbeat_interval=agent.heartbeat_interval,
            check_sync_interval=agent.check_sync_interval,
            max_concurrent_checks=agent.max_concurrent_checks,
            log_level=agent.log_level,
            report_batch_size=agent.report_batch_size,
            report_interval=agent.report_interval,
            report_process_interval=agent.report_process_interval,
            report_max_queue_size=agent.report_max_queue_size,
            report_max_files_per_batch=agent.report_max_files_per_batch,
            report_backpressure_threshold=agent.report_backpressure_threshold,
            watchdog_interval=agent.watchdog_interval,
            watchdog_stall_threshold=agent.watchdog_stall_threshold,
            queue_depth=agent.queue_depth,
            checks_executed_total=agent.checks_executed_total,
            stored_reports_count=agent.stored_reports_count,
            stored_reports_oldest_timestamp=agent.stored_reports_oldest_timestamp,
            dead_letter_count=agent.dead_letter_count,
            status_reason=agent.status_reason,
            api_key_last_used=agent.api_key_last_used,
            success_rate=agent.success_rate,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
            is_online=agent.is_online,
            uptime_seconds=agent.uptime_seconds,
            check_count=check_count,
        )

    @staticmethod
    async def get_hostnames_by_ids(
        db: AsyncSession, agent_ids: Sequence[UUID]
    ) -> dict[UUID, str]:
        """
        Resolve many agent hostnames in one query.

        For labelling a page of rows that each name an agent — one round-trip
        instead of one per distinct agent.

        Args:
            db: Database session
            agent_ids: Agent UUIDs to resolve

        Returns:
            agent_id -> hostname, omitting ids that no longer exist
        """
        return await AgentCRUD.get_hostnames_by_ids(db, agent_ids)

    @staticmethod
    async def get_agent(db: AsyncSession, agent_id: UUID) -> AgentResponse:
        """The agent as the web seam holds it — a DTO, not the ORM row.

        Returns AgentResponse rather than AgentInDB because the seam needs the
        computed fields too: check_count, is_online and uptime_seconds. They are
        derived HERE, from the eagerly-loaded row, because `agent.checks` is a
        relationship — a schema that merely declared it would lazy-load outside
        the session and raise MissingGreenlet.

        get_agent_by_id stays ORM; the approve/reject/pause lifecycle methods
        read it and mutate the row they get back. Raises the same
        AgentNotFoundException, so callers keep their error path.
        """
        return AgentCoreService.to_response(
            await AgentCoreService.get_agent_by_id(db, agent_id)
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
    ) -> AgentOption:
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
            The agent the check is assigned to (its id is what to persist)

        Raises:
            AgentNotFoundException: If agent not found, or no agents available
                for replicate/distribute modes.
        """
        if assignment_mode == "manual":
            if agent_id is None:
                raise AgentNotFoundException("Agent required for manual assignment")
            return AgentOption.model_validate(
                await AgentCoreService.get_agent_by_id(db, agent_id)
            )

        # replicate / distribute: pick first available agent as placeholder
        agents, _ = await AgentCoreService.list_agents(
            db, limit=1, exclude_pending=True
        )
        if not agents:
            raise AgentNotFoundException("No agents available")
        return AgentOption.model_validate(agents[0])

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
    async def recover_agent_key(
        db: AsyncSession, agent_id: UUID, recovery_secret: str | None
    ) -> str:
        """Issue an active agent a fresh key, once it proves its recovery secret.

        The caller has already proved the registration key, which every agent
        install shares; the secret, issued to this agent alone at registration,
        is what binds the recovery to it. An unknown agent, an agent registered
        before secrets existed (no hash), a missing secret and a wrong one are
        all refused the same way, after the same digest comparison.

        Raises:
            AuthenticationException: If the agent and secret do not match
            AuthorizationException: If the agent is not active
        """
        agent = await AgentCRUD.get_by_id_with_checks(db, agent_id)
        stored = agent.recovery_secret_hash if agent is not None else None
        presented = _recovery_secret_digest(recovery_secret or "")
        # Always one constant-time comparison, against a digest no secret has
        # when there is nothing stored, so the refusal takes the same path.
        matches = hmac.compare_digest(
            presented.encode(), (stored or _NO_SECRET_DIGEST).encode()
        )
        if agent is None or not stored or not recovery_secret or not matches:
            logger.warning(
                "SECURITY AUDIT: Agent key recovery refused",
                extra={
                    "event": "agent_key_recovery_refused",
                    "agent_id": str(agent_id),
                    "agent_known": agent is not None,
                    "secret_presented": bool(recovery_secret),
                    "secret_on_record": bool(stored),
                },
            )
            raise AuthenticationException("Invalid agent recovery credentials")
        if agent.approval_status != "active":
            logger.warning(
                "SECURITY AUDIT: Agent key recovery refused",
                extra={
                    "event": "agent_key_recovery_refused",
                    "agent_id": str(agent_id),
                    "approval_status": agent.approval_status,
                },
            )
            raise AuthorizationException(
                "Agent must be active to recover key. "
                f"Current status: {agent.approval_status}"
            )
        _, new_key = await AgentCoreService.regenerate_agent_key(db, agent_id)
        logger.warning(
            "SECURITY AUDIT: Agent key recovered",
            extra={"event": "agent_key_recovered", "agent_id": str(agent_id)},
        )
        return new_key

    @staticmethod
    async def update_agent_response(
        db: AsyncSession, agent_id: UUID, data: AgentUpdate
    ) -> AgentResponse:
        """Update an agent and return it in its API shape."""
        return AgentCoreService.to_response(
            await AgentCoreService.update_agent(db, agent_id, data)
        )

    @staticmethod
    async def list_agent_options(db: AsyncSession) -> list[AgentOption]:
        """Every admitted agent as a picker option (pending and rejected excluded)."""
        agents, _ = await AgentCoreService.list_agents(
            db, limit=1000, exclude_pending=True
        )
        return [AgentOption.model_validate(agent) for agent in agents]

    @staticmethod
    async def list_agent_tags(db: AsyncSession) -> list[str]:
        """The distinct tags carried by admitted agents, sorted."""
        agents, _ = await AgentCoreService.list_agents(
            db, limit=1000, exclude_pending=True
        )
        # post-db-filter: the tag list is aggregated across rows
        return sorted(
            {tag.strip() for agent in agents for tag in agent.tags or [] if tag.strip()}
        )

    @staticmethod
    async def list_agent_responses(
        db: AsyncSession,
        *,
        active_only: bool = False,
        search: str | None = None,
        offset: int = 0,
        limit: int = 100,
        exclude_pending: bool = False,
    ) -> tuple[list[AgentResponse], int]:
        """A page of agents in their API shape, and the total that match."""
        agents, total = await AgentCoreService.list_agents(
            db,
            active_only=active_only,
            search=search,
            offset=offset,
            limit=limit,
            exclude_pending=exclude_pending,
        )
        return [AgentCoreService.to_response(agent) for agent in agents], total

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
            raise DuplicateResourceException(
                f"Agent with name '{data.agent_name}' already exists"
            )

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

        agent = await AgentCRUD.add_agent(db, agent)

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
    ) -> tuple[AgentOption, str]:
        """
        Register a new agent with pending approval status.

        This is used during agent self-registration process. Agent is created
        with approval_status='pending' and no agent_name. Admin must approve
        the agent before it can operate.

        The agent is issued a random recovery secret, kept only as its digest:
        key recovery needs it, so the shared registration key alone can never
        mint a key for an agent.

        Args:
            db: Database session
            hostname: Agent hostname
            ip_address: Agent IP address
            version: Agent version
            tags: Agent tags

        Returns:
            Created agent with pending status, and its plaintext recovery
            secret (returned once; not stored)
        """
        recovery_secret = f"luxswirl_rs_{secrets.token_hex(32)}"
        agent = Agent(
            agent_name=None,  # Will be set during approval
            hostname=hostname,
            ip_address=ip_address,
            version=version,
            tags=tags or None,
            approval_status="pending",
            first_seen=utc_now(),
            last_seen=utc_now(),
            recovery_secret_hash=_recovery_secret_digest(recovery_secret),
        )

        agent = await AgentCRUD.add_agent(db, agent)

        logger.info(
            "Agent registered",
            extra={
                "agent_id": str(agent.id),
                "hostname": hostname,
                "ip_address": ip_address,
            },
        )
        return AgentOption.model_validate(agent), recovery_secret

    @staticmethod
    async def update_agent(
        db: AsyncSession, agent_id: UUID, data: AgentUpdate
    ) -> Agent:
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

        agent = await AgentCRUD.save_and_reload_agent(db, agent)

        logger.info(
            "Updated agent",
            extra={"agent_name": agent.agent_name, "agent_id": str(agent.id)},
        )
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
            skip=offset,
            limit=limit,
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

        AgentCoreService._apply_heartbeat(agent, heartbeat)
        await AgentCRUD.save_agent(db, agent)

        metric = AgentCoreService._heartbeat_metric(agent, heartbeat)
        await AgentCRUD.add_metric(db, metric)

        # Update Prometheus metrics in-memory (instant update on ingestion)

        try:
            MetricsCollectorCoreService.update_agent_status(agent)
            MetricsCollectorCoreService.update_agent_metrics(metric, agent)
        # swallowed-exceptions: metrics are downstream of ingestion — a
        # Prometheus update must not reject the agent's heartbeat.
        except Exception:
            logger.exception("Error updating Prometheus metrics")

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
    def _apply_heartbeat(agent: Agent, heartbeat: AgentHeartbeat) -> None:
        """Copy what a heartbeat reports about the agent onto its row.

        Identity fields (hostname, ip, version, tags) change only when the
        heartbeat sends a value; health fields always take the latest report.
        """
        # Liveness: the heartbeat is the only writer of last_seen and the run id
        agent.last_seen = heartbeat.timestamp
        if heartbeat.agent_run_id:
            agent.agent_run_id = str(heartbeat.agent_run_id)

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
        agent.stored_reports_oldest_timestamp = (
            heartbeat.stored_reports_oldest_timestamp
        )
        agent.dead_letter_count = heartbeat.dead_letter_count

        # Update resource monitoring fields (SWIRL-57)
        agent.open_file_descriptors = heartbeat.open_file_descriptors
        agent.fd_limit_soft = heartbeat.fd_limit_soft
        agent.fd_usage_percent = heartbeat.fd_usage_percent
        agent.subprocess_count = heartbeat.subprocess_count

    @staticmethod
    def _heartbeat_metric(agent: Agent, heartbeat: AgentHeartbeat) -> AgentMetric:
        """The heartbeat as an AgentMetric row, for time-series storage."""
        return AgentMetric(
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

    @staticmethod
    async def process_heartbeat(
        db: AsyncSession,
        agent_id: UUID,
        heartbeat: AgentHeartbeat,
        *,
        using_agent_key: bool,
    ) -> AgentHeartbeatResponse:
        """Apply heartbeat update and build the response payload.

        Caller handles HTTP-layer auth and passes ``using_agent_key`` to gate
        job dispatch. This method handles persistence, gating, and response
        construction.
        """
        agent = await AgentCoreService.update_from_heartbeat(db, agent_id, heartbeat)

        config_version = (
            agent.checks_updated_at.isoformat() if agent.checks_updated_at else None
        )
        heartbeat_interval = (
            agent.heartbeat_interval or settings.server.default_heartbeat_interval
        )
        check_sync_interval = (
            agent.check_sync_interval or settings.server.default_check_sync_interval
        )

        jobs_list: list[dict[str, Any]] = []
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

        await AgentCRUD.delete_agent(db, agent)

        logger.info(
            "Deleted agent",
            extra={"agent_name": agent.agent_name, "agent_id": str(agent_id)},
        )

    @staticmethod
    async def generate_agent_key(
        db: AsyncSession, agent_id: UUID
    ) -> tuple[AgentOption, str]:
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

        # Raises AgentNotFoundException for an unknown agent.
        await AgentCoreService.get_agent_by_id(db, agent_id)

        new_key = f"luxswirl_ak_{secrets.token_hex(16)}"
        salt = bcrypt.gensalt()
        key_hash = bcrypt.hashpw(new_key.encode("utf-8"), salt).decode("utf-8")

        # Claimed in one statement that only matches an agent with no key, so two
        # concurrent requests cannot both issue one (the loser's key would be dead).
        agent = await AgentCRUD.claim_initial_api_key(db, agent_id, key_hash, utc_now())
        if agent is None:
            raise ValueError("Agent already has an API key. Use regenerate instead.")
        logger.info(
            "Generated initial API key for agent",
            extra={"agent_id": str(agent_id)},
        )

        return AgentOption.model_validate(agent), new_key

    @staticmethod
    async def regenerate_agent_key(
        db: AsyncSession, agent_id: UUID
    ) -> tuple[AgentOption, str]:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info(
            "Regenerated API key for agent",
            extra={"agent_id": str(agent_id)},
        )

        return AgentOption.model_validate(agent), new_key

    @staticmethod
    async def revoke_agent_key(db: AsyncSession, agent_id: UUID) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info(
            "Revoked API key for agent",
            extra={"agent_id": str(agent_id)},
        )

        return AgentOption.model_validate(agent)

    # ====================================================================
    # State transition methods
    # ====================================================================

    @staticmethod
    async def approve_agent(
        db: AsyncSession, agent_id: UUID
    ) -> tuple[AgentOption, str]:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info("Approved agent", extra={"agent_id": str(agent_id)})

        return AgentOption.model_validate(agent), api_key

    @staticmethod
    async def reject_agent(
        db: AsyncSession, agent_id: UUID, reason: str | None = None
    ) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info("Rejected agent", extra={"agent_id": str(agent_id)})

        return AgentOption.model_validate(agent)

    @staticmethod
    async def pause_agent(
        db: AsyncSession, agent_id: UUID, reason: str | None = None
    ) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info("Paused agent", extra={"agent_id": str(agent_id)})

        return AgentOption.model_validate(agent)

    @staticmethod
    async def resume_agent(db: AsyncSession, agent_id: UUID) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info("Resumed agent", extra={"agent_id": str(agent_id)})

        return AgentOption.model_validate(agent)

    @staticmethod
    async def disable_agent(
        db: AsyncSession, agent_id: UUID, reason: str | None = None
    ) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info("Disabled agent", extra={"agent_id": str(agent_id)})

        return AgentOption.model_validate(agent)

    @staticmethod
    async def enable_agent(db: AsyncSession, agent_id: UUID) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info("Enabled agent", extra={"agent_id": str(agent_id)})

        return AgentOption.model_validate(agent)

    @staticmethod
    async def force_reload(db: AsyncSession, agent_id: UUID) -> AgentOption:
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

        await AgentCRUD.save_agent(db, agent)
        logger.info(
            "Forced config reload for agent",
            extra={"agent_id": str(agent_id)},
        )

        return AgentOption.model_validate(agent)

    @staticmethod
    async def get_pending_agents(db: AsyncSession) -> list[PendingAgent]:
        """
        Get all agents with pending approval status.

        Args:
            db: Database session

        Returns:
            List of pending agents
        """
        return [
            PendingAgent.model_validate(row) for row in await AgentCRUD.list_pending(db)
        ]

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
        agents, _ = await AgentCoreService.list_agents(
            db, limit=10000, exclude_pending=True
        )

        tags_set = set()
        for agent in agents:
            for tag in agent.tags or []:
                tag = tag.strip()
                if tag:
                    tags_set.add(tag)

        return sorted(tags_set)

    @staticmethod
    async def get_admitted_agents(db: AsyncSession) -> list[AgentOption]:
        """
        Get all admitted agents (active, paused, or disabled).

        Excludes pending and rejected. See AgentCRUD.get_admitted_agents for
        why "admitted" rather than "approved".

        Args:
            db: Database session

        Returns:
            List of admitted Agent objects ordered by name
        """
        return [
            AgentOption.model_validate(row)
            for row in await AgentCRUD.get_admitted_agents(db)
        ]

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
        db: AsyncSession, agent_ids: Sequence[UUID], cutoff_time: datetime
    ) -> dict[UUID, list[AgentMetricSample]]:
        """Each listed agent's metrics since cutoff, oldest first; one query."""
        by_agent: dict[UUID, list[AgentMetricSample]] = {
            agent_id: [] for agent_id in agent_ids
        }
        for row in await AgentCRUD.get_agent_metrics(db, agent_ids, cutoff_time):
            by_agent[row.agent_id].append(AgentMetricSample.model_validate(row))
        return by_agent
