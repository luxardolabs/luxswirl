"""
Alert-Notification Provider mapping - many-to-many relationship.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    SerializerMixin,
    TableNameMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.models.alert_model import Alert
    from app.models.notification_provider_model import NotificationProvider


class AlertNotificationMapping(
    Base, UUIDPrimaryKeyMixin, TimestampMixin, TableNameMixin, SerializerMixin
):
    """
    Maps alerts to notification providers (many-to-many).

    Each alert can send to multiple providers, and each provider
    can be used by multiple alerts.
    """

    __tablename__ = "alert_notification_mappings"
    __table_args__ = (
        UniqueConstraint(
            "alert_id",
            "notification_provider_id",
            name="uq_alert_notification",
        ),
        Index("idx_alert_notification_alert", "alert_id"),
        Index("idx_alert_notification_provider", "notification_provider_id"),
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

    # Per-mapping configuration
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether this specific alert-provider mapping is enabled",
    )

    # Relationships
    alert: Mapped[Alert] = relationship(
        "Alert",
        back_populates="notification_mappings",
        lazy="selectin",
    )

    notification_provider: Mapped[NotificationProvider] = relationship(
        "NotificationProvider",
        back_populates="alert_mappings",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<AlertNotificationMapping(id={self.id}, "
            f"alert_id={self.alert_id}, "
            f"provider_id={self.notification_provider_id}, "
            f"enabled={self.is_enabled})>"
        )
