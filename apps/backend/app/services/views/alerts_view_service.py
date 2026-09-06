"""
Web alerts service - aggregates alert data for web UI.

Provides web-specific alert operations by delegating to core services.
This layer isolates web routers from direct core service dependencies.
"""

from uuid import UUID

from fastapi import Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AuthedUser
from app.models.enum_model import AlertTriggerType
from app.schemas.alert_schema import AlertCreate, AlertUpdate
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.notification_core_service import NotificationCoreService

logger = get_logger("luxswirl.web.services.alerts")


class AlertsViewService:
    """Web service for alert operations."""

    # How long the "snooze" button silences an alert-check pair. A presentation policy
    # (what the button promises the user), so it lives in the view, not in core.
    SNOOZE_MINUTES = 15

    @staticmethod
    async def get_alert_form_data(
        db: AsyncSession, request: Request, alert_id: UUID | None = None
    ) -> dict:
        """
        Get all data needed for alert create/edit form.

        Args:
            db: Database session
            request: Request, for reversing route names into URLs
            alert_id: Alert ID for edit form, None for create form

        Returns:
            Dict with alert, providers, checks, form_action
        """
        # Get alert if editing
        alert = None
        if alert_id:
            alert = await AlertCoreService.get_alert_by_id(db, alert_id)

        # Get available notification providers
        providers, _ = await NotificationCoreService.list_providers(db, limit=1000)

        # Get all checks for assignment
        checks = await CheckCoreService.list_all_checks(db)

        # The form posts to update when editing, create otherwise. Reverse the
        # route name so the path lives with the route, not in the template.
        form_action = (
            request.url_for("web_update_alert", alert_id=alert_id).path
            if alert_id
            else request.url_for("web_create_alert").path
        )

        return {
            "alert": alert,
            "providers": providers,
            "checks": checks,
            "form_action": form_action,
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
        return await AlertCoreService.update_alert(
            db, alert_id, AlertUpdate(is_enabled=enabled)
        )

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
    async def snooze_alert_check(
        db: AsyncSession, alert_id: UUID, check_id: UUID, current_user: AuthedUser
    ) -> str:
        """Snooze an alert-check pair for the standard window.

        Returns the toast message the caller shows.
        """
        minutes = AlertsViewService.SNOOZE_MINUTES
        mapping = await AlertCoreService.snooze_alert_check(
            db, alert_id, check_id, minutes=minutes
        )
        logger.info(
            "User snoozed alert-check relationship",
            extra={
                "username": current_user.username,
                "user_id": str(current_user.id),
                "snoozed_until": str(mapping.snoozed_until),
                "alert_name": mapping.alert.name,
                "alert_id": str(mapping.alert.id),
                "check_name": mapping.check.display_name,
                "check_id": str(mapping.check.id),
            },
        )
        return f"Snoozed for {minutes} minutes"

    @staticmethod
    async def unsnooze_alert_check(
        db: AsyncSession, alert_id: UUID, check_id: UUID, current_user: AuthedUser
    ) -> str:
        """Clear a snooze so notifications resume; returns the toast message."""
        await AlertCoreService.unsnooze_alert_check(db, alert_id, check_id)
        logger.info(
            "User un-snoozed alert-check relationship",
            extra={"username": current_user.username, "user_id": str(current_user.id)},
        )
        return "Snooze cleared — notifications resumed"

    @staticmethod
    async def toggle_alert_enabled(db: AsyncSession, alert_id: UUID) -> HTMLResponse:
        """Flip an alert's enabled flag and ask the page to refresh."""
        alert = await AlertCoreService.get_alert_by_id(db, alert_id)
        updated = await AlertsViewService.set_alert_enabled(
            db, alert_id, not alert.is_enabled
        )
        logger.info(
            "Toggled alert",
            extra={
                "alert_name": updated.name,
                "alert_id": str(updated.id),
                "is_enabled": updated.is_enabled,
            },
        )
        return HTMLResponse(
            content="", status_code=200, headers={"HX-Trigger": "refreshPage"}
        )
