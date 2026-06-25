"""
Notification Provider model - stores notification provider configurations.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Index, String
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
from app.models.enum_model import NotificationProviderType

if TYPE_CHECKING:
    from app.models.alert_notification_mapping_model import AlertNotificationMapping
    from app.models.notification_log_model import NotificationLog


class NotificationProvider(
    Base,
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TableNameMixin,
    SoftDeleteMixin,
    SerializerMixin,
):
    """
    Notification Provider model - stores notification provider configurations.

    Each provider represents a notification destination (email, webhook, etc.)
    with its specific configuration stored in JSONB for flexibility.
    """

    __tablename__ = "notification_providers"
    __table_args__ = (
        Index("idx_notification_providers_type", "provider_type"),
        Index("idx_notification_providers_deleted", "deleted_at"),
    )

    # Provider type (email, webhook, homeassistant, etc.)
    provider_type: Mapped[NotificationProviderType] = mapped_column(
        str_enum(NotificationProviderType, 50),
        nullable=False,
        comment="Type of notification provider (email, webhook, homeassistant, etc.)",
    )

    # Friendly name for UI display
    friendly_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="User-friendly name for this provider instance (e.g., 'Google SMTP', 'PagerDuty')",
    )

    # Provider-specific configuration stored as JSONB
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="Provider-specific configuration (JSONB for flexibility)",
    )

    # Enabled flag - controls whether this provider can send notifications
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether this provider is enabled (can send notifications)",
    )

    # Default enabled flag
    is_default_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether this provider is enabled by default for new checks",
    )

    # Rate limiting configuration
    rate_limit_count: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Maximum notifications per rate_limit_window_minutes (NULL = no limit)",
    )

    rate_limit_window_minutes: Mapped[int | None] = mapped_column(
        nullable=True,
        default=60,
        server_default="60",
        comment="Time window for rate limiting in minutes",
    )

    # Relationships
    alert_mappings: Mapped[list[AlertNotificationMapping]] = relationship(
        "AlertNotificationMapping",
        back_populates="notification_provider",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    notification_logs: Mapped[list[NotificationLog]] = relationship(
        "NotificationLog",
        back_populates="notification_provider",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<NotificationProvider(id={self.id}, "
            f"type={self.provider_type!r}, "
            f"name={self.friendly_name!r})>"
        )
