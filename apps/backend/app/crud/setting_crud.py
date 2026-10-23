"""
Setting CRUD - database queries for settings.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting_model import Setting


class SettingCRUD:
    """Database queries for settings."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_setting(db: AsyncSession, setting: Setting) -> Setting:
        db.add(setting)
        await db.flush()
        await db.refresh(setting)
        return setting

    @staticmethod
    async def save_setting(db: AsyncSession, setting: Setting) -> Setting:
        """Write a setting's changed value; the refreshed row."""
        await db.flush()
        await db.refresh(setting)
        return setting

    @staticmethod
    async def get_by_key(db: AsyncSession, key: str) -> Setting | None:
        result = await db.execute(select(Setting).where(Setting.key == key))
        return result.scalar_one_or_none()

    @staticmethod
    async def existing_keys(db: AsyncSession) -> set[str]:
        """Every setting key currently stored, in one round-trip.

        Seeding asks "does this key exist?" once per default; asking the
        database that separately each time costs one query per default at
        every startup.
        """
        # unbounded-reads: the settings catalogue (one row per default)
        result = await db.execute(select(Setting.key))
        return set(result.scalars())

    @staticmethod
    async def list_by_category(db: AsyncSession, category: str) -> Sequence[Setting]:
        result = await db.execute(
            select(Setting).where(Setting.category == category).order_by(Setting.key)
        )
        return result.scalars().all()
