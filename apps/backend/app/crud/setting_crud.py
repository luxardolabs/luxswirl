"""
Setting CRUD - database queries for settings.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting_model import Setting


class SettingCRUD:
    """Database queries for settings."""

    @staticmethod
    async def get_by_key(db: AsyncSession, key: str) -> Setting | None:
        result = await db.execute(select(Setting).where(Setting.key == key))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_category(db: AsyncSession, category: str) -> Sequence[Setting]:
        result = await db.execute(
            select(Setting).where(Setting.category == category).order_by(Setting.key)
        )
        return result.scalars().all()

    @staticmethod
    async def list_all(db: AsyncSession) -> Sequence[Setting]:
        result = await db.execute(select(Setting).order_by(Setting.category, Setting.key))
        return result.scalars().all()
