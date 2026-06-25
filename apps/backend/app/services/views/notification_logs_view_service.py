"""
Notification Logs Service - web-specific business logic for notification logs page.
"""

from datetime import UTC, datetime
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import NotificationStatus
from app.schemas.pagination_schema import build_pagination
from app.services.core.notification_log_core_service import NotificationLogCoreService
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.services.notification_logs_view")


class NotificationLogRow:
    """View model for notification log row in the table."""

    def __init__(
        self,
        log_id: UUID,
        sent_at: datetime,
        alert_name: str,
        alert_id: UUID,
        snoozed_until: datetime | None,
        provider_name: str,
        check_name: str,
        check_type: str,
        check_target: str,
        status: str,
        error_message: str | None,
        check_success: bool,
        check_latency_ms: float | None,
        check_id: UUID | None,
    ):
        self.id = log_id
        self.sent_at = sent_at
        self.alert_name = alert_name
        self.alert_id = alert_id
        self.snoozed_until = snoozed_until
        self.provider_name = provider_name
        self.display_name = check_name
        self.check_name = check_name
        self.check_type = check_type
        self.check_target = check_target
        self.status = status
        self.error_message = error_message
        self.check_success = check_success
        self.check_latency_ms = check_latency_ms
        self.check_id = check_id

    @property
    def row_target(self) -> str:
        """HTMX target selector for re-rendering this row in place."""
        return f"#notification-log-row-{self.id}"

    @property
    def check_detail_url(self) -> str | None:
        """Link to the check detail panel, or None if the check is gone."""
        return f"/check/{self.check_id}" if self.check_id else None

    @property
    def snooze_url(self) -> str | None:
        """Snooze add/clear endpoint for this alert+check, or None if no check."""
        if not self.check_id:
            return None
        return f"/alerts/snooze?alert_id={self.alert_id}&check_id={self.check_id}&log_id={self.id}"

    @property
    def snooze_duration(self) -> str | None:
        """
        Calculate human-readable snooze duration.

        Returns:
            String like "15m", "1h 30m", "2h", or None if not snoozed
        """
        if not self.snoozed_until:
            return None

        now = datetime.now(UTC)

        # If snooze has expired, return None
        if self.snoozed_until <= now:
            return None

        delta = self.snoozed_until - now
        total_minutes = int(delta.total_seconds() / 60)

        if total_minutes < 60:
            return f"{total_minutes}m"

        hours = total_minutes // 60
        minutes = total_minutes % 60

        if minutes == 0:
            return f"{hours}h"

        return f"{hours}h {minutes}m"


class NotificationLogsViewService:
    """Service for notification logs page."""

    @staticmethod
    async def get_notification_logs(
        db: AsyncSession,
        status: str | None = None,
        alert_id: UUID | None = None,
        notification_provider_id: UUID | None = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[NotificationLogRow], int]:
        """
        Get paginated notification logs with filters.

        Args:
            db: Database session
            status: Filter by status (sent, failed, etc.)
            alert_id: Filter by alert ID
            notification_provider_id: Filter by provider ID
            search: Search in alert name, provider name, check name
            limit: Number of logs per page
            offset: Offset for pagination

        Returns:
            Tuple of (list of NotificationLogRow, total count)
        """
        rows, total = await NotificationLogCoreService.get_logs_paginated(
            db,
            status=status,
            alert_id=alert_id,
            notification_provider_id=notification_provider_id,
            search=search,
            limit=limit,
            offset=offset,
        )

        # Convert to view models
        log_rows = [
            NotificationLogRow(
                log_id=row.id,
                sent_at=row.sent_at,
                alert_name=row.alert_name,
                alert_id=row.alert_id,
                snoozed_until=row.snoozed_until,
                provider_name=row.provider_name,
                check_name=row.display_name,
                check_type=row.check_type,
                check_target=row.target,
                status=row.status,
                error_message=row.error_message,
                check_success=row.success if row.success is not None else False,
                check_latency_ms=row.latency_ms,
                check_id=row.check_id,
            )
            for row in rows
        ]

        return log_rows, total

    @staticmethod
    async def get_log_row_by_id(db: AsyncSession, log_id: UUID) -> NotificationLogRow | None:
        """Fetch a single notification log row as a view model for partial re-render."""
        row = await NotificationLogCoreService.get_log_by_id(db, log_id)
        if row is None:
            return None
        return NotificationLogRow(
            log_id=row.id,
            sent_at=row.sent_at,
            alert_name=row.alert_name,
            alert_id=row.alert_id,
            snoozed_until=row.snoozed_until,
            provider_name=row.provider_name,
            check_name=row.display_name,
            check_type=row.check_type,
            check_target=row.target,
            status=row.status,
            error_message=row.error_message,
            check_success=row.success if row.success is not None else False,
            check_latency_ms=row.latency_ms,
            check_id=row.check_id,
        )

    @staticmethod
    async def get_all_alerts(db: AsyncSession) -> list[tuple[str, str]]:
        """
        Get all alerts for filter dropdown.

        Returns:
            List of (alert_id, alert_name) tuples
        """
        return await NotificationLogCoreService.get_all_alerts_for_dropdown(db)

    @staticmethod
    async def get_all_providers(db: AsyncSession) -> list[tuple[str, str]]:
        """
        Get all notification providers for filter dropdown.

        Returns:
            List of (provider_id, provider_name) tuples
        """
        return await NotificationLogCoreService.get_all_providers_for_dropdown(db)

    @staticmethod
    async def get_status_counts(db: AsyncSession) -> dict:
        """
        Get counts by status for summary stats.

        Returns:
            Dict with one key per known status (sent, failed, retrying, rate_limited,
            deduplicated, suppressed) plus a `total` key.
        """
        return await NotificationLogCoreService.get_status_counts(db)

    @staticmethod
    async def get_setting(db, key: str, default):
        return await SettingsCoreService.get_setting(db, key, default)

    @staticmethod
    async def build_logs_page_context(
        db,
        *,
        request,
        current_user,
        status: NotificationStatus | None,
        alert_id: UUID | None,
        notification_provider_id: UUID | None,
        search: str | None,
        page: int,
        per_page: int | None,
    ) -> dict:
        """Build the full template context for /notification-logs."""
        if per_page is None:
            per_page = await NotificationLogsViewService.get_setting(
                db, "general.default_page_size", 50
            )

        offset = (page - 1) * per_page
        # Boundary already validated/normalized: enum-or-None, UUID-or-None. The
        # string-based crud + template comparisons want plain strings.
        alert_str = str(alert_id) if alert_id else None
        provider_str = str(notification_provider_id) if notification_provider_id else None

        status_counts = await NotificationLogsViewService.get_status_counts(db)
        log_rows, total = await NotificationLogsViewService.get_notification_logs(
            db=db,
            status=status,
            alert_id=alert_id,
            notification_provider_id=notification_provider_id,
            search=search,
            limit=per_page,
            offset=offset,
        )
        all_alerts = await NotificationLogsViewService.get_all_alerts(db)
        all_providers = await NotificationLogsViewService.get_all_providers(db)

        filters = {
            "status": status,
            "alert_id": alert_str,
            "notification_provider_id": provider_str,
            "search": search,
        }
        pagination = build_pagination(page=page, per_page=per_page, total=total, filters=filters)

        # Filter dropdown options come from the canonical enum, formatted for display.
        status_options = [
            {"value": s.value, "label": s.value.replace("_", " ").title()}
            for s in NotificationStatus
        ]

        return {
            "request": request,
            "current_user": current_user,
            "status_counts": status_counts,
            "status_options": status_options,
            "log_rows": log_rows,
            "filters": filters,
            "all_alerts": all_alerts,
            "all_providers": all_providers,
            "pagination": pagination,
            "page_title": "Notification Logs",
        }
