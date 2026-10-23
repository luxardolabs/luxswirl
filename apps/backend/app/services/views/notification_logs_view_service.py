"""
Notification Logs Service - web-specific business logic for notification logs page.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import CheckType, NotificationStatus
from app.schemas.auth_schema import CurrentUserDTO
from app.schemas.notification_log_schema import NotificationLogEntry
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
        check_name: str | None,
        check_type: CheckType | None,
        check_target: str | None,
        status: NotificationStatus,
        error_message: str | None,
        check_success: bool,
        check_latency_ms: float | None,
        check_id: UUID | None,
        check_detail_url: str | None = None,
        snooze_url: str | None = None,
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
        # Finished hrefs, resolved by route name in _log_row; None when the
        # check is gone (no detail panel to open, nothing to snooze).
        self.check_detail_url = check_detail_url
        self.snooze_url = snooze_url

    @property
    def row_target(self) -> str:
        """HTMX target selector for re-rendering this row in place."""
        return f"#notification-log-row-{self.id}"

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


def _log_row(request: Request, entry: NotificationLogEntry) -> NotificationLogRow:
    """A log entry as its table row; a log with no check result reads as failed.

    The row's check-detail and snooze hrefs are resolved by route name here, so
    the paths have one home (the routers).
    """
    check_detail_url: str | None = None
    snooze_url: str | None = None
    if entry.check_id:
        check_detail_url = str(
            request.app.url_path_for("check_detail_panel", check_id=entry.check_id)
        )
        query = urlencode(
            {"alert_id": entry.alert_id, "check_id": entry.check_id, "log_id": entry.id}
        )
        snooze_url = f"{request.app.url_path_for('snooze_alert_check')}?{query}"
    return NotificationLogRow(
        log_id=entry.id,
        sent_at=entry.sent_at,
        alert_name=entry.alert_name,
        alert_id=entry.alert_id,
        snoozed_until=entry.snoozed_until,
        provider_name=entry.provider_name,
        check_name=(
            entry.display_name
            if entry.display_name or not entry.agent_name
            else f"Agent {entry.agent_name}"
        ),
        check_type=entry.check_type,
        check_target=entry.target,
        status=entry.status,
        error_message=entry.error_message,
        check_success=entry.success if entry.success is not None else False,
        check_latency_ms=entry.latency_ms,
        check_id=entry.check_id,
        check_detail_url=check_detail_url,
        snooze_url=snooze_url,
    )


class NotificationLogsViewService:
    """Service for notification logs page."""

    @staticmethod
    async def get_notification_logs(
        db: AsyncSession,
        request: Request,
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
            request: The request being rendered (resolves each row's hrefs)
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
        log_rows = [_log_row(request, entry) for entry in rows]

        return log_rows, total

    @staticmethod
    async def get_log_row_by_id(
        db: AsyncSession, request: Request, log_id: UUID
    ) -> NotificationLogRow | None:
        """Fetch a single notification log row as a view model for partial re-render."""
        row = await NotificationLogCoreService.get_log_by_id(db, log_id)
        if row is None:
            return None
        return _log_row(request, row)

    @staticmethod
    async def build_logs_page_context(
        db: AsyncSession,
        *,
        request: Request,
        current_user: CurrentUserDTO,
        status: NotificationStatus | None,
        alert_id: UUID | None,
        notification_provider_id: UUID | None,
        search: str | None,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """Build the full template context for /notification-logs."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )

        offset = (page - 1) * per_page
        # Boundary already validated/normalized: enum-or-None, UUID-or-None. The
        # string-based crud + template comparisons want plain strings.
        alert_str = str(alert_id) if alert_id else None
        provider_str = (
            str(notification_provider_id) if notification_provider_id else None
        )

        status_counts = await NotificationLogCoreService.get_status_counts(db)
        log_rows, total = await NotificationLogsViewService.get_notification_logs(
            db=db,
            request=request,
            status=status,
            alert_id=alert_id,
            notification_provider_id=notification_provider_id,
            search=search,
            limit=per_page,
            offset=offset,
        )
        all_alerts = await NotificationLogCoreService.get_all_alerts_for_dropdown(db)
        all_providers = await NotificationLogCoreService.get_all_providers_for_dropdown(
            db
        )

        filters = {
            "status": status,
            "alert_id": alert_str,
            "notification_provider_id": provider_str,
            "search": search,
        }
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )

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
            # The page's own path: filters and pagination re-request it.
            "list_url": str(request.app.url_path_for("notification_logs_page")),
            "page_title": "Notification Logs",
        }
