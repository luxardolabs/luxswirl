"""
API token model - a user's bearer credential for the admin API.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ARRAY, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_model import UUIDBaseModel


class ApiToken(UUIDBaseModel):
    """
    API token model - one of a user's tokens for `/api/v1/admin`.

    A token acts as its user, narrowed by its scopes: it may do what both the
    user's role and its scopes allow, and is refused once the user is inactive. Only the SHA-256 of the token is stored; the plaintext
    is shown once, at creation.
    """

    __tablename__ = "api_tokens"
    __table_args__ = (Index("idx_api_tokens_user_id", "user_id"),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="The user this token acts as",
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="What the token is for, named by its owner",
    )

    token_hash: Mapped[str] = mapped_column(
        String(64),  # SHA-256 produces 64 hex chars
        unique=True,
        nullable=False,
        index=True,
        comment="SHA-256 hash of the token",
    )

    token_prefix: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        comment="The token's first characters, to recognise it in a list",
    )

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)),
        nullable=False,
        comment="What the token may do: family:action scopes narrowing its user's role",
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When this token last authenticated a request",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When this token stops working (NULL = never)",
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When this token was revoked (NULL = not revoked)",
    )
