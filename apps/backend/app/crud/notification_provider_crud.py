"""
NotificationProvider CRUD - database queries for notification providers.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_provider_model import NotificationProvider


class NotificationProviderCRUD:
    """Database queries for notification providers."""

    @staticmethod
    async def get_by_id(
        db: AsyncSession, provider_id: UUID, include_deleted: bool = False
    ) -> NotificationProvider | None:
        """Fetch a notification provider by id."""
        query = select(NotificationProvider).where(NotificationProvider.id == provider_id)
        if not include_deleted:
            query = query.where(NotificationProvider.deleted_at.is_(None))
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def list_paginated(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        provider_type: str | None = None,
        include_deleted: bool = False,
    ) -> tuple[Sequence[NotificationProvider], int]:
        """Paginated list of providers with optional filters. Returns (rows, total)."""
        query = select(NotificationProvider)
        if not include_deleted:
            query = query.where(NotificationProvider.deleted_at.is_(None))
        if provider_type:
            query = query.where(NotificationProvider.provider_type == provider_type)

        count_query = select(func.count()).select_from(query.subquery())
        total = await db.scalar(count_query) or 0

        query = query.order_by(NotificationProvider.id).offset(skip).limit(limit)
        result = await db.execute(query)
        return result.scalars().all(), total
