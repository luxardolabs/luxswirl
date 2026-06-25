"""
User CRUD - database queries for users.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.user_model import User


class UserCRUD:
    """Database queries for users."""

    @staticmethod
    async def get_by_username(db: AsyncSession, username: str) -> User | None:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: UUID) -> User | None:
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def count_active(db: AsyncSession) -> int:
        result = await db.execute(select(func.count(User.id)).where(User.is_active.is_(True)))
        return result.scalar_one()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
        role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[User], int]:
        """Paginated list of users with optional filters."""
        stmt = select(User)
        if role:
            stmt = stmt.where(User.role == role)
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    User.username.ilike(pattern),
                    User.full_name.ilike(pattern),
                )
            )

        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await db.execute(stmt.order_by(User.created_at.desc()).offset(skip).limit(limit))
        return result.scalars().all(), total

    @staticmethod
    async def count_total(db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(User))
        return result.scalar_one()

    @staticmethod
    async def count_by_role(db: AsyncSession, role: str) -> int:
        result = await db.execute(select(func.count(User.id)).where(User.role == role))
        return result.scalar_one()

    @staticmethod
    async def count_locked(db: AsyncSession) -> int:
        """Users with locked_until still in the future."""
        result = await db.execute(select(func.count(User.id)).where(User.locked_until > utc_now()))
        return result.scalar_one()

    @staticmethod
    async def get_first_admin(db: AsyncSession) -> User | None:
        """Return the first admin user (or None if none exist)."""
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        return result.scalar_one_or_none()
