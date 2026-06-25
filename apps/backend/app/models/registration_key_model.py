"""
Registration Key model - shared tokens for agent registration.
"""

from datetime import datetime

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import UUIDBaseModel


class RegistrationKey(UUIDBaseModel):
    """
    Registration Key model - shared tokens that allow agents to register.

    These are admin-managed tokens that agents use for:
    1. Initial registration (first contact)
    2. Key recovery (if agent loses its agent-specific key)

    Unlike agent-specific keys, these are shared and can be used by multiple agents.
    """

    __tablename__ = "registration_keys"
    __table_args__ = (
        Index("idx_registration_keys_revoked_at", "revoked_at"),
        Index("idx_registration_keys_last_used_at", "last_used_at"),
    )

    # Human-readable name/description
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Name/description for this registration key",
    )

    # Bcrypt hash of the key (never store plaintext)
    key_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="Bcrypt hash of the registration key",
    )

    # Optional description/notes
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional notes about this key's purpose",
    )

    # Key lifecycle tracking
    created_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Admin user who created this key (future use)",
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When this key was last used for authentication",
    )

    usage_count: Mapped[int] = mapped_column(
        nullable=False,
        server_default="0",
        comment="Number of times this key has been used",
    )

    # Revocation (soft delete)
    revoked_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When this key was revoked (NULL = active)",
    )

    revoked_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Admin user who revoked this key (future use)",
    )

    revoked_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Reason for revocation",
    )

    @property
    def is_active(self) -> bool:
        """Check if key is active (not revoked)."""
        return self.revoked_at is None

    @property
    def status(self) -> str:
        """Get human-readable status."""
        if self.revoked_at:
            return "revoked"
        return "active"
