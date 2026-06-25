"""
Notification Log model - audit trail of sent notifications.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import (
    Base,
    SerializerMixin,
    TableNameMixin,
    TimestampMixin,
    str_enum,
)
from app.models.enum_model import NotificationStatus

if TYPE_CHECKING:
    from app.models.alert_model import Alert
    from app.models.notification_provider_model import NotificationProvider


class NotificationLog(Base, TimestampMixin, TableNameMixin, SerializerMixin):
    """
    Notification Log - tracks all notification attempts.

    Provides audit trail and helps with de-duplication and rate limiting.

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

    # Reference to check_results (no FK - both tables are hypertables with retention)
    check_result_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        comment="Check result ID (reference only, no FK for TimescaleDB compression)",
    )

    check_result_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Check result timestamp (reference only, no FK for TimescaleDB compression)",
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
        comment="Status of notification (sent, failed, retrying, rate_limited, deduplicated, suppressed)",
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
        comment="When notification was sent",
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
