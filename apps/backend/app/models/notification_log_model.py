"""
Notification Log model - audit trail of sent notifications.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Sequence,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base_model import (
    Base,
    SerializerMixin,
    TableNameMixin,
    TimestampMixin,
    str_enum,
)
from app.models.enum_model import NotificationStatus

# Numbers the rows in the order they are decided (`decision_seq`).
DECISION_SEQUENCE = Sequence("notification_decision_order")

if TYPE_CHECKING:
    from app.models.alert_model import Alert
    from app.models.notification_provider_model import NotificationProvider


class NotificationLog(Base, TimestampMixin, TableNameMixin, SerializerMixin):
    """
    Notification Log - every notification an alert decided, and its delivery.

    Also the delivery outbox: alert evaluation writes a `pending` row, with
    the notification's `payload`, in the transaction that decides it, and the
    delivery worker (`app/background/notification_delivery_worker.py`)
    claims it, delivers it outside any transaction and records the outcome.
    Provides the audit trail and the memory de-duplication and rate limiting
    read.

    Plain table (not a hypertable): low-volume audit data queried by alert /
    provider / status, with DELETE-based retention via cleanup_notification_logs.
    """

    __tablename__ = "notification_logs"
    __table_args__ = (
        # No FK to check_results: it's a compressed hypertable, and FKs into a
        # compressed hypertable are unsupported — so the check fields below are
        # denormalized onto this table instead.
        Index("idx_notification_logs_alert", "alert_id"),
        Index("idx_notification_logs_provider", "notification_provider_id"),
        Index(
            "idx_notification_logs_check_result",
            "check_result_id",
            "check_result_timestamp",
        ),
        Index("idx_notification_logs_status", "status"),
        # The delivery worker's claim: rows still awaiting delivery, by when
        # they are next due.
        Index(
            "idx_notification_logs_undelivered",
            "next_attempt_at",
            postgresql_where="status IN ('pending', 'sending', 'retrying')",
        ),
        Index("idx_notification_logs_alert_agent", "alert_id", "agent_id"),
        Index("idx_notification_logs_sent_at", "sent_at"),
        # Composite index for rate limiting queries
        Index(
            "idx_notification_logs_provider_sent",
            "notification_provider_id",
            "sent_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
        comment="UUID primary key",
    )

    # Foreign keys
    alert_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to alerts table",
    )

    notification_provider_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("notification_providers.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to notification_providers table",
    )

    # Reference to check_results (no FK - both tables are hypertables with retention).
    # NULL for a notification about missing data (no_data, agent_offline): no
    # result triggered it.
    check_result_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="Check result ID (reference only, no FK for TimescaleDB compression)",
    )

    check_result_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Check result timestamp (reference only, no FK for TimescaleDB compression)",
    )

    agent_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="Agent an agent_offline notification is about (NULL for check notifications)",
    )

    # Denormalized fields (avoids LATERAL join against check_results)
    check_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        comment="Check ID (denormalized — avoids join through check_results to reach checks)",
    )

    check_success: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="Whether the check passed (denormalized from check_results)",
    )

    check_latency_ms: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Check latency in ms (denormalized from check_results)",
    )

    # Notification details
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus, 50),
        nullable=False,
        comment="Status of notification (pending, sending, retrying, sent, failed, rate_limited, deduplicated, suppressed)",
    )

    # The delivery outbox
    decision_seq: Mapped[int] = mapped_column(
        BigInteger,
        # Each insert takes the sequence's next value; the column's default in
        # the database (migration 020) does the same for a row written without it.
        DECISION_SEQUENCE,
        nullable=False,
        comment="Order the rows were decided in: one alert's notifications about one subject are delivered to a provider in this order",
    )

    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="The notification to deliver (NotificationContext) while it awaits delivery; cleared once delivery ends",
    )

    attempts: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="Delivery attempts made",
    )

    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a pending or retrying row is next due for delivery",
    )

    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a sending row's claim lapses and another worker may deliver it",
    )

    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When delivery ended (sent, failed, suppressed or rate limited by the worker)",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if notification failed",
    )

    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the notification was decided (delivered_at is when its delivery ended)",
    )

    # Provider response data (for debugging)
    response_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Provider-specific response data (JSONB)",
    )

    # De-duplication tracking
    notification_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Hash of notification content for de-duplication",
    )

    # Resend tracking
    is_resend: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether this is a resend of a previous notification",
    )

    resend_count: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="How many times this notification has been resent",
    )

    # Relationships
    alert: Mapped[Alert] = relationship(
        "Alert",
        back_populates="notification_logs",
        lazy="selectin",
    )

    notification_provider: Mapped[NotificationProvider] = relationship(
        "NotificationProvider",
        back_populates="notification_logs",
        lazy="selectin",
    )

    # Note: check_result relationship removed - no FK between hypertables
    # Use check_result_id and check_result_timestamp for manual lookups if needed

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<NotificationLog(id={self.id}, "
            f"alert_id={self.alert_id}, "
            f"provider_id={self.notification_provider_id}, "
            f"status={self.status!r})>"
        )
