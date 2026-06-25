"""
Job model - Tasks dispatched to agents for discovery, diagnostics, and maintenance.
"""

from datetime import datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.datetime_utils import utc_now
from app.models.base import Base, TimestampMixin, str_enum
from app.models.enum_model import JobStatus, JobType


class Job(Base, TimestampMixin):
    """
    Job model for agent task dispatch.

    Jobs are tasks sent to agents (or executed on server) for operations like:
    - Network discovery (scan subnets for devices)
    - Port scanning (test connectivity)
    - DNS lookups (resolve hostnames)
    - Diagnostics (MTR, traceroute)
    - Cloud inventory (list resources)

    Status Flow:
        pending → assigned → running → completed/failed/cancelled

    Jobs are auto-purged after retention period (default 7 days).
    """

    __tablename__ = "jobs"

    # Primary key
    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
        comment="Job UUID",
    )

    # Job definition
    job_type: Mapped[JobType] = mapped_column(
        str_enum(JobType, 50),
        nullable=False,
        index=True,
        comment="Type of job (network_scan, port_scan, etc.)",
    )

    agent_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        index=True,
        comment="Agent to run job on (NULL = run on server)",
    )

    params: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Job-specific parameters (JSON)",
    )

    # Status and priority
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, 20),
        nullable=False,
        default=JobStatus.PENDING,
        index=True,
        comment="Job status (pending/assigned/running/completed/failed/cancelled)",
    )

    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Job priority (higher = runs first)",
    )

    # Timestamps
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When job was assigned to agent",
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When job execution started",
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When job execution completed",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="When to auto-purge this job (created_at + retention_days)",
    )

    # Results
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Job result data (JSON)",
    )

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if job failed",
    )

    # Metadata
    created_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="User who created this job",
    )

    tags: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Tags for organizing/filtering jobs (JSON array)",
    )

    # Future fields (Phase 2: Scheduling & Automation)
    schedule: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Scheduling config (NULL = one-time job)",
    )

    automation_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Automation rules (NULL = manual processing)",
    )

    parent_job_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="Parent job ID for recurring job instances",
    )

    # Indexes for common queries
    __table_args__ = (
        # Fast lookup for pending jobs by agent
        Index(
            "idx_jobs_agent_status_priority",
            "agent_id",
            "status",
            "priority",
            postgresql_where=(status.in_(["pending", "assigned"])),
        ),
        # Fast cleanup of expired jobs
        Index(
            "idx_jobs_expires_status",
            "expires_at",
            "status",
            postgresql_where=(status.in_(["completed", "failed", "cancelled"])),
        ),
        # Lookup by type and status
        Index("idx_jobs_type_status", "job_type", "status"),
    )

    @property
    def duration_seconds(self) -> float | None:
        """Calculate job execution duration."""
        # Check if job stored its own accurate duration in result
        if self.result:
            # Different job types may use different field names
            if "scan_duration_seconds" in self.result:
                return cast(float, self.result["scan_duration_seconds"])
            elif "duration_seconds" in self.result:
                return cast(float, self.result["duration_seconds"])
            elif "duration" in self.result:
                return cast(float, self.result["duration"])

        # Fallback to timestamp calculation
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def is_terminal(self) -> bool:
        """Check if job is in a terminal state (completed/failed/cancelled)."""
        return self.status in ("completed", "failed", "cancelled")

    @property
    def is_active(self) -> bool:
        """Check if job is currently active (running)."""
        return self.status == "running"

    @property
    def is_pending(self) -> bool:
        """Check if job is waiting to be picked up."""
        return self.status in ("pending", "assigned")

    # ------------------------------------------------------------------
    # Runner (agent or server) — a first-class concept, not a scattered
    # string. A Job runs on a runner: a specific agent, or the server
    # itself (agent_id IS NULL). Everything goes through here instead of
    # comparing to NULL / "server" inline.
    # ------------------------------------------------------------------
    SERVER_RUNNER: ClassVar[str] = "server"
    """UI/filter token for the server runner (a Job with no assigned agent)."""

    @property
    def runs_on_server(self) -> bool:
        """True when this job runs on the server itself (no assigned agent)."""
        return self.agent_id is None

    @property
    def runner_token(self) -> str:
        """The runner as a UI/URL token: the agent UUID string, or SERVER_RUNNER."""
        return str(self.agent_id) if self.agent_id else self.SERVER_RUNNER

    def assign(self) -> None:
        """Mark job as assigned to agent."""
        self.status = JobStatus.ASSIGNED
        self.assigned_at = utc_now()

    def start(self) -> None:
        """Mark job as started."""
        self.status = JobStatus.RUNNING
        self.started_at = utc_now()

    def complete(self, result: dict[str, Any]) -> None:
        """Mark job as completed with result."""
        self.status = JobStatus.COMPLETED
        self.completed_at = utc_now()
        self.result = result

    def fail(self, error: str) -> None:
        """Mark job as failed with error message."""
        self.status = JobStatus.FAILED
        self.completed_at = utc_now()
        self.error = error

    def cancel(self) -> None:
        """Cancel the job."""
        self.status = JobStatus.CANCELLED
        self.completed_at = utc_now()

    def set_expiration(self, retention_days: int = 7) -> None:
        """Set expiration timestamp based on retention policy."""
        self.expires_at = utc_now() + timedelta(days=retention_days)

    def __repr__(self) -> str:
        """String representation."""
        return f"<Job(id={self.id}, type={self.job_type}, agent={self.agent_id}, status={self.status})>"
