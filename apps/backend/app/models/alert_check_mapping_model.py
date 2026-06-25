"""
Alert-Check mapping - many-to-many relationship for per-check alert assignments.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, UniqueConstraint, Uuid
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
    from app.models.check_model import Check


class AlertCheckMapping(Base, UUIDPrimaryKeyMixin, TimestampMixin, TableNameMixin, SerializerMixin):
    """
    Maps alerts to specific checks (many-to-many).

    Allows per-check alert assignments in addition to global alerts.
    """

    __tablename__ = "alert_check_mappings"
    __table_args__ = (
        UniqueConstraint(
            "alert_id",
            "check_id",
            name="uq_alert_check",
        ),
        Index("idx_alert_check_alert", "alert_id"),
        Index("idx_alert_check_check", "check_id"),
    )

    # Foreign keys
    alert_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to alerts table",
    )

    check_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("checks.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to checks table (UUID)",
    )

    # Per-mapping configuration
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether this specific alert-check mapping is enabled",
    )

    # Override alert settings per check
    override_resend_interval: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Override resend interval for this specific check (minutes)",
    )

    override_max_resends: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Override max resends for this specific check",
    )

    # Snooze functionality - pause notifications for this specific alert-check relationship
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Notifications for this alert-check relationship are snoozed until this timestamp - NULL means not snoozed",
    )

    # Relationships
    alert: Mapped[Alert] = relationship(
        "Alert",
        back_populates="check_mappings",
        lazy="selectin",
    )

    check: Mapped[Check] = relationship(
        "Check",
        back_populates="alert_mappings",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<AlertCheckMapping(id={self.id}, "
            f"alert_id={self.alert_id}, "
            f"check_id={self.check_id}, "
            f"enabled={self.is_enabled})>"
        )

    @property
    def snooze_duration(self) -> str | None:
        """
        Calculate human-readable snooze duration for this alert-check relationship.

        Returns:
            String like "15m", "1h 30m", "2h", or None if not snoozed/expired
        """
        if not self.snoozed_until:
            return None

        now = datetime.now(UTC)

        # If snooze has expired, return None
        if self.snoozed_until <= now:
            return None

        delta = self.snoozed_until - now
        total_minutes = int(delta.total_seconds() / 60)

        if total_minutes < 60:
            return f"{total_minutes}m"

        hours = total_minutes // 60
        minutes = total_minutes % 60

        if minutes == 0:
            return f"{hours}h"

        return f"{hours}h {minutes}m"
