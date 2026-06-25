"""
Scheduler models for async job scheduler.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDBaseModel, str_enum
from app.models.enum_model import (
    SchedulerExecutionStatus,
    SchedulerJobCategory,
    SchedulerTriggerType,
)


class JobConfiguration(UUIDBaseModel):
    """
    Store job-specific configuration with scheduling.

    Each job has:
    - A unique job_key string for human-readable identification
    - A function_name mapping to a registered Python function
    - Scheduling config (interval, cron, or manual trigger)
    - Lease-based single-runner semantics (SELECT FOR UPDATE SKIP LOCKED)
    - Retry with exponential backoff
    - Runtime stats (last_run, total_runs, failed_runs, avg_duration)
    """

    __tablename__ = "job_configurations"
    __table_args__ = (
        Index("idx_job_configurations_enabled", "enabled"),
        Index("idx_job_configurations_next_run", "next_run_at"),
        Index("idx_job_configurations_lease", "lease_expires_at"),
    )

    job_key: Mapped[str] = mapped_column(
        String(191), unique=True, nullable=False, comment="Unique job identifier"
    )
    function_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Python function name to execute"
    )

    # Display info
    display_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Human-readable job name"
    )
    description: Mapped[str | None] = mapped_column(Text, comment="Job description")
    category: Mapped[SchedulerJobCategory] = mapped_column(
        str_enum(SchedulerJobCategory, 50),
        nullable=False,
        comment="Job category (cleanup, monitoring, system)",
    )

    # Configuration
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="Whether job is enabled")
    parameters: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="Parameters passed to job function"
    )

    # Scheduling
    trigger_type: Mapped[SchedulerTriggerType] = mapped_column(
        str_enum(SchedulerTriggerType, 20),
        nullable=False,
        comment="Trigger type: interval, cron, manual",
    )
    interval_seconds: Mapped[int | None] = mapped_column(
        Integer, comment="Interval in seconds (for interval triggers)"
    )
    cron_expression: Mapped[str | None] = mapped_column(
        String(100), comment="Cron expression (for cron triggers)"
    )
    timezone: Mapped[str] = mapped_column(
        String(50), default="UTC", comment="Timezone for cron expressions"
    )

    # Next run tracking (NULL for manual jobs)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When job should next run"
    )
    max_runtime_seconds: Mapped[int] = mapped_column(
        Integer, default=300, comment="Maximum execution time before timeout"
    )
    jitter_ms: Mapped[int] = mapped_column(
        Integer, default=0, comment="Random jitter in milliseconds to spread load"
    )

    # Lease for single-runner semantics
    lease_token: Mapped[UUID | None] = mapped_column(
        Uuid, nullable=True, comment="Current lease token (UUID)"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When current lease expires"
    )

    # Retry configuration
    retry_limit: Mapped[int] = mapped_column(
        Integer, default=3, comment="Max retries before auto-disable (0=infinite)"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="Current consecutive failure count"
    )
    backoff_seconds: Mapped[int] = mapped_column(
        Integer, default=30, comment="Base backoff time for exponential retry"
    )

    # Notifications
    notify_on_failure: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="Log warning on failure"
    )

    # Stats (updated after each run)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When job last ran"
    )
    last_status: Mapped[SchedulerExecutionStatus | None] = mapped_column(
        str_enum(SchedulerExecutionStatus, 20),
        comment="Last execution status",
    )
    total_runs: Mapped[int] = mapped_column(Integer, default=0, comment="Total successful runs")
    failed_runs: Mapped[int] = mapped_column(Integer, default=0, comment="Total failed runs")
    average_duration: Mapped[float | None] = mapped_column(
        Float, comment="Average execution duration in seconds"
    )


class JobExecution(Base):
    """
    Track job execution history.

    Uses plain Base (not UUIDBaseModel) because it doesn't need
    auto-generated table name or created_at/updated_at timestamps.
    """

    __tablename__ = "job_executions"
    __table_args__ = (
        Index("idx_job_executions_job_key", "job_key"),
        Index("idx_job_executions_started_at", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, comment="Execution UUID")
    job_key: Mapped[str] = mapped_column(String(191), nullable=False, comment="Job identifier")
    job_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Job display name at time of execution"
    )
    category: Mapped[SchedulerJobCategory | None] = mapped_column(
        str_enum(SchedulerJobCategory, 50),
        comment="Job category at time of execution",
    )

    # Execution details
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="When execution started"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When execution completed"
    )
    status: Mapped[SchedulerExecutionStatus] = mapped_column(
        str_enum(SchedulerExecutionStatus, 20),
        nullable=False,
        default=SchedulerExecutionStatus.RUNNING,
        comment="Execution status",
    )

    # Results
    error_message: Mapped[str | None] = mapped_column(Text, comment="Error message if failed")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, comment="Job output/result data")
    records_processed: Mapped[int | None] = mapped_column(
        Integer, comment="Number of records processed"
    )

    # Performance
    duration_seconds: Mapped[float | None] = mapped_column(
        Float, comment="Execution duration in seconds"
    )

    @property
    def duration(self) -> float | None:
        """Calculate duration if completed."""
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None
