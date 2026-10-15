"""
RegistrationKey CRUD - database queries for registration keys.
"""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.registration_key_model import RegistrationKey


class RegistrationKeyCRUD:
    """Database queries for registration keys."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_key(db: AsyncSession, key: RegistrationKey) -> RegistrationKey:
        db.add(key)
        await db.flush()
        await db.refresh(key)
        return key

    @staticmethod
    async def save_key(db: AsyncSession, key: RegistrationKey) -> RegistrationKey:
        """Write a key's changed fields; the refreshed row."""
        await db.flush()
        await db.refresh(key)
        return key

    @staticmethod
    async def hard_delete_key(db: AsyncSession, key: RegistrationKey) -> None:
        await db.delete(key)

    @staticmethod
    async def record_usage(db: AsyncSession, key_id: UUID) -> RegistrationKey | None:
        """Count one use and stamp it, in one statement (no lost update under
        concurrent registrations); the updated row, or None if the key was
        revoked or deleted since it was read. Re-checking revoked_at here is what
        refuses a key revoked while its bcrypt check ran."""
        return await db.scalar(
            update(RegistrationKey)
            .where(
                RegistrationKey.id == key_id,
                RegistrationKey.revoked_at.is_(None),
            )
            .values(usage_count=RegistrationKey.usage_count + 1, last_used_at=utc_now())
            .returning(RegistrationKey)
            .execution_options(populate_existing=True)
        )

    @staticmethod
    async def get_by_id(db: AsyncSession, key_id: UUID) -> RegistrationKey | None:
        """Fetch a registration key by id."""
        result = await db.execute(
            select(RegistrationKey).where(RegistrationKey.id == key_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
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
