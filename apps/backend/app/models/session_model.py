"""
Session model - represents user sessions for authentication.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import UUIDBaseModel

if TYPE_CHECKING:
    from app.models.user_model import User


class Session(UUIDBaseModel):
    """
    Session model - stores active user sessions.

    Sessions are identified by secure random tokens stored in HTTP-only cookies.
    Sessions expire after a configurable period (default 7 days).
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_user_id", "user_id"),
        Index("idx_sessions_token_hash", "token_hash"),
        Index("idx_sessions_expires_at", "expires_at"),
        Index("idx_sessions_last_activity", "last_activity_at"),
    )

    # Foreign key to user
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Foreign key to users table (UUID)",
    )

    # Session token (hashed for security - store SHA-256 hash)
    token_hash: Mapped[str] = mapped_column(
        String(64),  # SHA-256 produces 64 hex chars
        unique=True,
        nullable=False,
        index=True,
        comment="SHA-256 hash of session token",
    )

    # Session expiration
    expires_at: Mapped[datetime] = mapped_column(
        nullable=False,
        index=True,
        comment="When this session expires",
    )

    # Session metadata (for security audit and user visibility)
    ip_address: Mapped[str | None] = mapped_column(
        String(45),  # IPv6 max length
        nullable=True,
        comment="IP address from which session was created",
    )

    user_agent: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="User agent string from session creation",
    )

    last_activity_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        index=True,
        comment="Timestamp of last activity on this session (for idle timeout)",
    )

    # Relationships
    user: Mapped[User] = relationship(
        "User",
        back_populates="sessions",
        lazy="selectin",
    )

    @property
    def is_expired(self) -> bool:
        """Check if session has expired."""
        from app.core.datetime_utils import utc_now

        return utc_now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if session is valid (not expired and user is active)."""
        if self.is_expired:
            return False
        if hasattr(self, "user") and self.user:
            return self.user.is_active and not self.user.is_locked
        return True

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return f"<Session(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})>"
