"""
Web alerts service - aggregates alert data for web UI.

Provides web-specific alert operations by delegating to core services.
This layer isolates web routers from direct core service dependencies.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enum_model import AlertTriggerType
from app.schemas.alert_schema import AlertCreate, AlertUpdate
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.notification_core_service import NotificationCoreService

logger = get_logger("luxswirl.web.services.alerts")


class AlertsViewService:
    """Web service for alert operations."""

    @staticmethod
    async def get_alert_form_data(db: AsyncSession, alert_id: UUID | None = None) -> dict:
        """
        Get all data needed for alert create/edit form.

        Args:
            db: Database session
            alert_id: Alert ID for edit form, None for create form

        Returns:
            Dict with alert, providers, checks
        """
        # Get alert if editing
        alert = None
        if alert_id:
            alert = await AlertCoreService.get_alert_by_id(db, alert_id)

        # Get available notification providers
        providers, _ = await NotificationCoreService.list_providers(db, limit=1000)

        # Get all checks for assignment
        checks = await CheckCoreService.list_all_checks(db)

        return {
            "alert": alert,
            "providers": providers,
            "checks": checks,
        }

    @staticmethod
    async def create_alert(
        db: AsyncSession,
        *,
        name: str,
        description: str | None,
        trigger_type: str,
        trigger_config: dict,
        is_enabled: bool,
        is_global: bool,
        notify_on_recovery: bool,
        resend_interval_minutes: int | None,
        max_resends: int | None,
        custom_subject: str | None,
        custom_message: str | None,
        notification_provider_ids: list[UUID],
        check_ids: list[UUID],
    ):
        """Build the AlertCreate DTO from raw form inputs and create the alert."""
        alert_data = AlertCreate(
            name=name,
            description=description or None,
            trigger_type=AlertTriggerType(trigger_type),
            trigger_config=trigger_config,
            is_enabled=is_enabled,
            is_global=is_global,
            notify_on_recovery=notify_on_recovery,
            resend_interval_minutes=resend_interval_minutes,
            max_resends=max_resends,
            custom_subject=custom_subject or None,
            custom_message=custom_message or None,
            notification_provider_ids=notification_provider_ids,
            check_ids=check_ids,
        )
        return await AlertCoreService.create_alert(db, alert_data)

    @staticmethod
    async def update_alert(
        db: AsyncSession,
        alert_id: UUID,
        *,
        name: str,
        description: str | None,
        is_enabled: bool,
        notify_on_recovery: bool,
        resend_interval_minutes: int | None,
        max_resends: int | None,
        custom_subject: str | None,
        custom_message: str | None,
        trigger_config: dict | None,
    ):
        """Build the AlertUpdate DTO from raw form inputs and update the alert."""
        update_data = AlertUpdate(
            name=name,
            description=description or None,
            is_enabled=is_enabled,
            notify_on_recovery=notify_on_recovery,
            resend_interval_minutes=resend_interval_minutes,
            max_resends=max_resends,
            custom_subject=custom_subject or None,
            custom_message=custom_message or None,
            trigger_config=trigger_config,
        )
        return await AlertCoreService.update_alert(db, alert_id, update_data)

    @staticmethod
    async def set_alert_enabled(db: AsyncSession, alert_id: UUID, enabled: bool):
        """Enable/disable an alert (builds the partial AlertUpdate internally)."""
        return await AlertCoreService.update_alert(db, alert_id, AlertUpdate(is_enabled=enabled))

    @staticmethod
    async def delete_alert(db: AsyncSession, alert_id: UUID):
        """
        Delete an alert rule (soft delete).

        Args:
            db: Database session
            alert_id: Alert ID to delete
        """
        await AlertCoreService.delete_alert(db, alert_id, hard_delete=False)

    @staticmethod
    async def get_alert_by_id(db: AsyncSession, alert_id: UUID):
        """
        Get a specific alert by ID.

        Args:
            db: Database session
            alert_id: Alert ID

        Returns:
            Alert model
        """
        return await AlertCoreService.get_alert_by_id(db, alert_id)

    @staticmethod
    async def snooze_alert_check(db, alert_id, check_id, minutes: int = 15):
        return await AlertCoreService.snooze_alert_check(db, alert_id, check_id, minutes=minutes)

    @staticmethod
    async def unsnooze_alert_check(db, alert_id, check_id):
        return await AlertCoreService.unsnooze_alert_check(db, alert_id, check_id)
