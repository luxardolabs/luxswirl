"""
StatusPage CRUD - database queries for status pages.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.status_page_model import StatusPage


class StatusPageCRUD:
    """Database queries for status pages."""

    # ── Writes (flushed; the session owner commits) ───────────────────────────

    @staticmethod
    async def add_page(db: AsyncSession, status_page: StatusPage) -> StatusPage:
        db.add(status_page)
        await db.flush()
        await db.refresh(status_page)
        return status_page

    @staticmethod
    async def save_page(db: AsyncSession, status_page: StatusPage) -> StatusPage:
        """Write a page's changed fields; the refreshed row."""
        await db.flush()
        await db.refresh(status_page)
        return status_page

    @staticmethod
    async def delete_page(db: AsyncSession, status_page: StatusPage) -> None:
        await db.delete(status_page)
        await db.flush()

    @staticmethod
    async def get_by_id(db: AsyncSession, status_page_id: UUID) -> StatusPage | None:
        result = await db.execute(
            select(StatusPage).where(StatusPage.id == status_page_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_slug(db: AsyncSession, slug: str) -> StatusPage | None:
        result = await db.execute(select(StatusPage).where(StatusPage.slug == slug))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        *,
        is_public: bool | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[StatusPage], int]:
        """Paginated list of status pages, optionally filtered by is_public."""
        query = select(StatusPage)
        count_query = select(func.count(StatusPage.id))
        if is_public is not None:
            query = query.where(StatusPage.is_public == is_public)
            count_query = count_query.where(StatusPage.is_public == is_public)

        total = (await db.execute(count_query)).scalar_one()
        result = await db.execute(
            query.order_by(StatusPage.name).limit(limit).offset(skip)
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def count_public(db: AsyncSession) -> int:
        result = await db.execute(
            select(func.count(StatusPage.id)).where(StatusPage.is_public.is_(True))
        )
        return result.scalar_one()

    @staticmethod
    async def count_private(db: AsyncSession) -> int:
        result = await db.execute(
            select(func.count(StatusPage.id)).where(StatusPage.is_public.is_(False))
        )
        return result.scalar_one()
