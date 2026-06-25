"""
Alert model - defines alert rules and triggers.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    SerializerMixin,
    SoftDeleteMixin,
    TableNameMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)
from app.models.enum_model import AlertTriggerType

if TYPE_CHECKING:
    from app.models.alert_check_mapping_model import AlertCheckMapping
    from app.models.alert_notification_mapping_model import AlertNotificationMapping
    from app.models.notification_log_model import NotificationLog


class Alert(
    Base,
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TableNameMixin,
    SoftDeleteMixin,
    SerializerMixin,
):
    """
    Alert model - defines when and how notifications should be triggered.

    Supports both global alerts (with filters) and per-check assignments.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("idx_alerts_trigger_type", "trigger_type"),
        Index("idx_alerts_enabled", "is_enabled"),
        Index("idx_alerts_deleted", "deleted_at"),
    )

    # Alert identification
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Name of the alert rule",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Detailed description of what this alert does",
    )

    # Trigger configuration
    trigger_type: Mapped[AlertTriggerType] = mapped_column(
        str_enum(AlertTriggerType, 50),
        nullable=False,
        comment="Type of trigger (status_change, threshold, repeated_failure, ssl_cert_expiry)",
    )

    trigger_config: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="Trigger-specific configuration (JSONB for flexibility)",
    )
    # Example trigger_config:
    # {
    #   "on_status": ["error"],  # Fire when check fails
    #   "consecutive_failures": 3,  # After 3 consecutive failures
    #   "check_filters": {  # Global filter - which checks this applies to
    #     "agent_ids": [1, 2],
    #     "check_types": ["http"],
    #     "check_names": ["api_*"],  # Wildcard support
    #     "tags": ["production"]
    #   }
    # }

    # Alert behavior
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether this alert is active",
    )

    is_global: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether this is a global alert (applies to filtered checks) or per-check",
    )

    # Recovery notification
    notify_on_recovery: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Send notification when service recovers (comes back up)",
    )

    # De-duplication settings
    resend_interval_minutes: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Resend notification after X minutes if still down (NULL = don't resend)",
    )

    max_resends: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Maximum number of resends (NULL = unlimited)",
    )

    # Custom message template
    custom_subject: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Custom subject line template (leave blank for default)",
    )

    custom_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Custom message body template (leave blank for default)",
    )

    # Relationships
    notification_mappings: Mapped[list[AlertNotificationMapping]] = relationship(
        "AlertNotificationMapping",
        back_populates="alert",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    check_mappings: Mapped[list[AlertCheckMapping]] = relationship(
        "AlertCheckMapping",
        back_populates="alert",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    notification_logs: Mapped[list[NotificationLog]] = relationship(
        "NotificationLog",
        back_populates="alert",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    @property
    def notification_provider_count(self) -> int:
        """Get count of notification providers attached to this alert."""
        return len(self.notification_mappings) if self.notification_mappings else 0

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<Alert(id={self.id}, "
            f"name={self.name!r}, "
            f"type={self.trigger_type!r}, "
            f"enabled={self.is_enabled})>"
        )
