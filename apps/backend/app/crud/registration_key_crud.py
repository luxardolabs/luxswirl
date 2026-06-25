"""
RegistrationKey CRUD - database queries for registration keys.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registration_key_model import RegistrationKey


class RegistrationKeyCRUD:
    """Database queries for registration keys."""

    @staticmethod
    async def get_by_id(db: AsyncSession, key_id: UUID) -> RegistrationKey | None:
        """Fetch a registration key by id."""
        result = await db.execute(select(RegistrationKey).where(RegistrationKey.id == key_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        include_revoked: bool = False,
    ) -> tuple[list[RegistrationKey], int]:
        """Paginated list of registration keys. Returns (rows, total)."""
        base = select(RegistrationKey)
        if not include_revoked:
            base = base.where(RegistrationKey.revoked_at.is_(None))

        total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0

        result = await db.execute(
            base.order_by(RegistrationKey.created_at.desc()).offset(skip).limit(limit)
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def list_active(db: AsyncSession) -> list[RegistrationKey]:
        """All non-revoked registration keys."""
        result = await db.execute(
            select(RegistrationKey).where(RegistrationKey.revoked_at.is_(None))
        )
        return list(result.scalars().all())
