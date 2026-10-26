"""
API token CRUD - database queries for users' admin-API tokens.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_token_model import ApiToken


class ApiTokenCRUD:
    """Database queries for API tokens."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_token(db: AsyncSession, token: ApiToken) -> ApiToken:
        db.add(token)
        await db.flush()
        await db.refresh(token)
        return token

    @staticmethod
    async def mark_used(db: AsyncSession, token: ApiToken, at: datetime) -> None:
        token.last_used_at = at
        await db.flush()

    @staticmethod
    async def mark_revoked(db: AsyncSession, token: ApiToken, at: datetime) -> None:
        token.revoked_at = at
        await db.flush()

    # ── Reads ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_by_hash(db: AsyncSession, token_hash: str) -> ApiToken | None:
        result = await db.execute(
            select(ApiToken).where(ApiToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(db: AsyncSession, token_id: UUID) -> ApiToken | None:
        return await db.get(ApiToken, token_id)

    @staticmethod
    async def list_unrevoked_for_user(
        db: AsyncSession, user_id: UUID
    ) -> list[ApiToken]:
        """A user's tokens that are not revoked (expired ones included), newest first."""
        result = await db.execute(
            select(ApiToken)
            .where(ApiToken.user_id == user_id)
            .where(ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
        return list(result.scalars().all())
