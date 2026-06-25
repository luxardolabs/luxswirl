"""
Agent model - represents monitoring agents.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.datetime_utils import utc_now
from app.models.base import UUIDBaseModel, str_enum
from app.models.enum_model import AgentApprovalStatus, AgentStatus

if TYPE_CHECKING:
    from app.models.agent_metric_model import AgentMetric
    from app.models.check_model import Check
    from app.models.check_result_model import CheckResult


class Agent(UUIDBaseModel):
    """
    Agent model - stores metadata about monitoring agents.

    An agent is a service that runs health checks and reports results
    to the server. Each agent has a UUID primary key (id) and a
    friendly, editable name (agent_name).
    """

    __tablename__ = "agents"
    __table_args__ = (
        Index("idx_agents_agent_name", "agent_name"),
        Index("idx_agents_last_seen", "last_seen"),
    )

    # Friendly name for the agent (editable, e.g., "london-agent", "production-monitor")
    # Can be NULL until administrator assigns a name during approval
    agent_name: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
        comment="Friendly name for the agent (editable, set during approval)",
    )

    # Current run ID (UUID) - changes each time agent restarts
    agent_run_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Current run ID (UUID), changes on agent restart",
    )

    # When agent was first seen
    first_seen: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default="NOW()",
        comment="Timestamp when agent was first seen",
    )

    # When agent was last seen (updated on every report)
    last_seen: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default="NOW()",
        comment="Timestamp when agent last reported",
    )

    # Metadata fields
    hostname: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Agent hostname",
    )

    ip_address: Mapped[str | None] = mapped_column(
        String(45),  # IPv6 max length
        nullable=True,
        comment="Agent IP address",
    )

    version: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Agent version",
    )

    tags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        nullable=True,
        comment="Tags for grouping/filtering agents",
    )

    # Health and status fields
    status: Mapped[AgentStatus | None] = mapped_column(
        str_enum(AgentStatus, 20),
        nullable=True,
        server_default="unknown",
        comment="Agent status: online, degraded, offline, unknown",
    )

    uptime_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Agent uptime in seconds (from last heartbeat)",
    )

    # Check execution statistics
    checks_total: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Total number of checks configured",
    )

    checks_active: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Number of checks currently running",
    )

    checks_executed_total: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Total checks executed since agent start",
    )

    checks_succeeded_total: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Total successful checks since agent start",
    )

    checks_failed_total: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Total failed checks since agent start",
    )

    # Performance metrics (from last heartbeat)
    cpu_percent: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="CPU usage percentage",
    )

    memory_mb: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Memory usage in MB",
    )

    queue_depth: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Current result queue depth",
    )

    # Error tracking
    last_error: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Last error message",
    )

    server_unreachable_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Number of failed server connection attempts",
    )

    stored_reports_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Number of stored reports waiting to send",
    )

    stored_reports_oldest_timestamp: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Timestamp of oldest stored report",
    )

    # Resource monitoring (SWIRL-57: detect subprocess/FD leaks)
    open_file_descriptors: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of open file descriptors",
    )

    fd_limit_soft: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Soft limit for file descriptors (ulimit)",
    )

    fd_usage_percent: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="FD usage percentage vs soft limit",
    )

    subprocess_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of child subprocesses",
    )

    # Config version tracking
    config_version: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Last known config version from agent",
    )

    checks_updated_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="Timestamp when checks were last modified (for config change detection)",
    )

    # Agent-specific intervals (if NULL, use global defaults)
    heartbeat_interval: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Heartbeat interval in seconds (NULL = use global default)",
    )

    check_sync_interval: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Check sync interval in seconds (NULL = use global default)",
    )

    # Reporter configuration (if NULL, use global defaults)
    report_interval: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Reporter batch send interval in seconds (NULL = use global default)",
    )

    report_batch_size: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Reporter batch size in results (NULL = use global default)",
    )

    report_max_files_per_batch: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max stored reports to process per batch (NULL = use global default)",
    )

    report_process_interval: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Interval for processing stored reports in seconds (NULL = use global default)",
    )

    report_max_queue_size: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum results queue size (NULL = use global default)",
    )

    report_backpressure_threshold: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Backpressure threshold 0.0-1.0 (NULL = use global default)",
    )

    # Performance tuning configuration (if NULL, use global defaults)
    max_concurrent_checks: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum concurrent checks (NULL = use global default)",
    )

    watchdog_interval: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Watchdog check interval in seconds (NULL = use global default)",
    )

    watchdog_stall_threshold: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Watchdog stall threshold count (NULL = use global default)",
    )

    # Logging configuration (if NULL, use global default)
    log_level: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL (NULL = use global default)",
    )

    # Approval workflow fields
    approval_status: Mapped[AgentApprovalStatus] = mapped_column(
        str_enum(AgentApprovalStatus, 20),
        nullable=False,
        server_default="pending",
        comment="Approval status: pending, active, paused, disabled, rejected",
    )

    api_key_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Bcrypt hash of agent-specific API key (NULL until approved)",
    )

    api_key_created_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When the API key was created/last regenerated",
    )

    api_key_last_used: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When the API key was last used for authentication",
    )

    approved_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When the agent was approved by admin",
    )

    approved_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Admin user who approved the agent (future use)",
    )

    status_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Reason for paused/disabled/rejected status",
    )

    status_changed_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When the approval_status was last changed",
    )

    status_changed_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Admin user who changed the status (future use)",
    )

    # Relationships
    checks: Mapped[list[Check]] = relationship(
        "Check",
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    check_results: Mapped[list[CheckResult]] = relationship(
        "CheckResult",
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="noload",  # Don't load by default (could be millions)
    )

    metrics: Mapped[list[AgentMetric]] = relationship(
        "AgentMetric",
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="noload",  # Don't load by default (time-series data)
    )

    @property
    def is_online(self, threshold_seconds: int = 300) -> bool:
        """
        Check if agent is currently online.

        Args:
            threshold_seconds: Seconds without report before considered offline

        Returns:
            True if agent is online
        """
        if not self.last_seen:
            return False
        elapsed = (utc_now() - self.last_seen).total_seconds()
        return elapsed < threshold_seconds

    @property
    def calculated_uptime_seconds(self) -> float:
        """Calculate agent uptime based on first_seen (alternative to reported uptime)."""
        if not self.first_seen:
            return 0.0
        return (utc_now() - self.first_seen).total_seconds()

    @property
    def success_rate(self) -> float:
        """Calculate check success rate percentage."""
        if not self.checks_executed_total or self.checks_executed_total == 0:
            return 0.0
        if self.checks_succeeded_total is None:
            return 0.0
        return (self.checks_succeeded_total / self.checks_executed_total) * 100
