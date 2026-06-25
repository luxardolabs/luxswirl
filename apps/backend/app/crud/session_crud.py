"""
Session CRUD - database queries for user sessions.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.session_model import Session


class SessionCRUD:
    """Database queries for user sessions."""

    @staticmethod
    async def delete_expired_before(db: AsyncSession, cutoff: datetime) -> int:
        """Delete sessions with expires_at older than cutoff. Returns rowcount."""
        result = await db.execute(delete(Session).where(Session.expires_at < cutoff))
        return result.rowcount or 0  # type: ignore[attr-defined]

    @staticmethod
    async def get_by_token_hash(db: AsyncSession, token_hash: str) -> Session | None:
        """Look up session by hashed token."""
        result = await db.execute(select(Session).where(Session.token_hash == token_hash))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_active_for_user(db: AsyncSession, user_id: UUID) -> list[Session]:
        """All non-expired sessions for a user, newest activity first."""
        result = await db.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .where(Session.expires_at > utc_now())
            .order_by(Session.last_activity_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_for_user(db: AsyncSession, session_id: UUID, user_id: UUID) -> Session | None:
        """Look up session that belongs to a user."""
        result = await db.execute(
            select(Session).where(Session.id == session_id, Session.user_id == user_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_expired(db: AsyncSession) -> list[Session]:
        """All sessions with expires_at <= now."""
        result = await db.execute(select(Session).where(Session.expires_at <= utc_now()))
        return list(result.scalars().all())
