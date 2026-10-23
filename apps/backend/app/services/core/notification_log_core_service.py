"""
NotificationLog service - business logic for notification log operations.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.notification_log_crud import NotificationLogCRUD
from app.models.enum_model import NotificationStatus
from app.schemas.notification_log_schema import NotificationLogEntry
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.notification_log")


class NotificationLogCoreService:
    """Service for notification log operations."""

    @staticmethod
    async def get_logs_paginated(
        db: AsyncSession,
        status: str | None = None,
        alert_id: UUID | None = None,
        notification_provider_id: UUID | None = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[NotificationLogEntry], int]:
        """
        Get paginated notification logs with filters and joins.

        Args:
            db: Database session
            status: Filter by status
            alert_id: Filter by alert ID
            notification_provider_id: Filter by provider ID
            search: Search in alert/provider/check names
            limit: Page size
            offset: Pagination offset

        Returns:
            Tuple of (the page's log entries, total count)
        """
        rows, total = await NotificationLogCRUD.get_logs_paginated(
            db,
            status=status,
            alert_id=alert_id,
            notification_provider_id=notification_provider_id,
            search=search,
            limit=limit,
            skip=offset,
        )
        return [
            NotificationLogEntry.model_validate(row._mapping) for row in rows
        ], total

    @staticmethod
    async def get_log_by_id(
        db: AsyncSession, log_id: UUID
    ) -> NotificationLogEntry | None:
        """One notification log, in the same shape as get_logs_paginated."""
        row = await NotificationLogCRUD.get_log_by_id(db, log_id)
        return (
            None if row is None else NotificationLogEntry.model_validate(row._mapping)
        )

    @staticmethod
    async def get_all_alerts_for_dropdown(db: AsyncSession) -> list[tuple[str, str]]:
        """
        Get all alerts for filter dropdown.

        Returns:
            List of (alert_id, alert_name) tuples
        """
        rows = await NotificationLogCRUD.get_all_alerts_for_dropdown(db)
        return [(str(row[0]), str(row[1])) for row in rows]

    @staticmethod
    async def get_all_providers_for_dropdown(db: AsyncSession) -> list[tuple[str, str]]:
        """
        Get all notification providers for filter dropdown.

        Returns:
            List of (provider_id, provider_name) tuples
        """
        rows = await NotificationLogCRUD.get_all_providers_for_dropdown(db)
        return [(str(row[0]), str(row[1])) for row in rows]

    @staticmethod
    async def get_status_counts(db: AsyncSession) -> dict[str, int]:
        """
        Get notification log counts by status.

        Returns:
            Dict with one key per `NotificationStatus` member plus a `total` key.
            Unknown statuses fall into `total` but get no dedicated key, so the
            template never sees a missing key.
        """
        rows = await NotificationLogCRUD.get_status_counts(db)

        counts: dict[str, int] = {s.value: 0 for s in NotificationStatus}
        counts["total"] = 0
        for row in rows:
            count_val = int(row[1])
            status = str(row[0])
            if status in counts:
                counts[status] = count_val
            counts["total"] += count_val

        return counts
