"""
Agent Metric model - time-series data for agent health metrics.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, str_enum
from app.models.enum_model import AgentStatus

if TYPE_CHECKING:
    from app.models.agent_model import Agent


class AgentMetric(Base):
    """
    Agent Metric model - stores time-series health metrics for agents.

    This is a TimescaleDB hypertable partitioned by timestamp for efficient
    time-series queries of agent performance and health data.
    """

    __tablename__ = "agent_metrics"
    __table_args__ = (
        Index("idx_agent_metrics_agent_time", "agent_id", "timestamp"),
        {"comment": "Time-series agent health metrics"},
    )

    # Foreign key to agent (part of composite primary key)
    agent_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        comment="Reference to agent (UUID)",
    )

    # Timestamp (partitioning column for hypertable, part of composite primary key)
    timestamp: Mapped[datetime] = mapped_column(
        primary_key=True,
        nullable=False,
        comment="Metric timestamp",
    )

    # Performance metrics
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
        comment="Result queue depth",
    )

    queue_max_size: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max queue size since last heartbeat",
    )

    # Check execution stats (since last heartbeat)
    checks_executed: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Checks executed since last heartbeat",
    )

    checks_succeeded: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Successful checks since last heartbeat",
    )

    checks_failed: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Failed checks since last heartbeat",
    )

    avg_check_duration_ms: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Average check duration in milliseconds",
    )

    # Health indicators
    status: Mapped[AgentStatus | None] = mapped_column(
        str_enum(AgentStatus, 20),
        nullable=True,
        comment="Agent status at this timestamp",
    )

    errors_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Error count since last heartbeat",
    )

    warnings_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        server_default="0",
        comment="Warning count since last heartbeat",
    )

    last_error: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
        comment="Last error message",
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
        comment="Soft limit for file descriptors",
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

    # Relationship
    agent: Mapped[Agent] = relationship(
        "Agent",
        back_populates="metrics",
        lazy="joined",
    )

    def __repr__(self) -> str:
        return f"<AgentMetric(agent_id={self.agent_id}, timestamp={self.timestamp}, status={self.status})>"
