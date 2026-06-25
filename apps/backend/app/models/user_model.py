"""
User model - represents authenticated users.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import UUIDBaseModel, str_enum
from app.models.enum_model import UserRole

if TYPE_CHECKING:
    from app.models.session_model import Session


class User(UUIDBaseModel):
    """
    User model - stores user accounts for authentication.

    Single-tenant: all users have access to all data, differentiated by role.
    Roles: admin, editor, viewer
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("idx_users_username", "username"),
        Index("idx_users_is_active", "is_active"),
        Index("idx_users_role", "role"),
    )

    # Authentication credentials
    username: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="Username for login (unique)",
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Bcrypt password hash",
    )

    # Role and status
    role: Mapped[UserRole] = mapped_column(
        str_enum(UserRole, 20),
        nullable=False,
        default=UserRole.VIEWER,
        server_default="viewer",
        comment="User role: admin, editor, viewer",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether user account is active",
    )

    # Profile information
    full_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Full name of user",
    )

    # Account metadata
    last_login_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="Timestamp of last successful login",
    )

    password_changed_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When password was last changed",
    )

    failed_login_attempts: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="Count of consecutive failed login attempts (reset on success)",
    )

    locked_until: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="Account locked until this timestamp (NULL = not locked)",
    )

    # Optional: force password change on first login
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether user must change password on next login",
    )

    # Admin tracking (for audit)
    created_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Username of admin who created this user",
    )

    # Relationships
    sessions: Mapped[list[Session]] = relationship(
        "Session",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    @property
    def is_locked(self) -> bool:
        """Check if account is currently locked."""
        if not self.locked_until:
            return False
        from app.core.datetime_utils import utc_now

        return utc_now() < self.locked_until

    @property
    def is_admin(self) -> bool:
        """Check if user has admin role."""
        return self.role == "admin"

    @property
    def is_editor(self) -> bool:
        """Check if user has editor role or higher."""
        return self.role in ("admin", "editor")

    @property
    def is_viewer(self) -> bool:
        """Check if user has at least viewer role."""
        return self.role in ("admin", "editor", "viewer")

    def can_edit(self) -> bool:
        """Check if user can edit resources."""
        return self.is_editor

    def can_manage_users(self) -> bool:
        """Check if user can manage other users."""
        return self.is_admin

    def __repr__(self) -> str:
        """Generate a helpful repr string."""
        return (
            f"<User(id={self.id}, username={self.username!r}, "
            f"role={self.role!r}, active={self.is_active})>"
        )
