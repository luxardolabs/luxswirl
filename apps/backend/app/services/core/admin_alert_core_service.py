"""
Admin alert core service - alert rules as the admin API serves them.

Every provider and check an alert is given is proved to exist first, so an
unknown id is a 422 naming it, never a foreign-key failure. A soft-deleted
alert is not found. Attaching an alert to a synthetic check, or detaching one,
changes how that check is watched, so it is an admin's, as every change to a
synthetic check is. A snooze is logged with who asked for it.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.models.enum_model import UserRole
from app.schemas.admin_alert_schema import (
    AdminAlertChecks,
    AdminAlertCreate,
    AdminAlertDetail,
    AdminAlertProviders,
    AdminAlertUpdate,
    AlertMappingChange,
    AlertPage,
)
from app.schemas.alert_schema import AlertCheckSnooze, AlertUpdate
from app.schemas.alert_trigger_config_schema import normalized_trigger_config
from app.schemas.pagination_schema import build_pagination
from app.services.core.alert_core_service import AlertCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.admin_alert")


class AdminAlertCoreService:
    """List, read, create, change and delete alerts, and their mappings."""

    @staticmethod
    async def get_alert(db: AsyncSession, alert_id: UUID) -> AdminAlertDetail:
        alert = await AlertCoreService.get_alert_response(db, alert_id)
        provider_ids, check_ids = await AlertCoreService.get_mapped_ids(db, alert_id)
        return AdminAlertDetail(
            **alert.model_dump(),
            notification_provider_ids=provider_ids,
            check_ids=check_ids,
        )

    @staticmethod
    async def list_alerts(
        db: AsyncSession,
        *,
        page: int,
        per_page: int,
        is_enabled: bool | None = None,
        is_global: bool | None = None,
    ) -> AlertPage:
        page = max(1, page)
        alerts, total = await AlertCoreService.list_alert_responses(
            db,
            skip=(page - 1) * per_page,
            limit=per_page,
            is_enabled=is_enabled,
            is_global=is_global,
        )
        return AlertPage.of(
            alerts, build_pagination(page=page, per_page=per_page, total=total)
        )

    @staticmethod
    async def create_alert(
        db: AsyncSession, role: UserRole, data: AdminAlertCreate
    ) -> AdminAlertDetail:
        await AlertCoreService.require_providers(db, data.notification_provider_ids)
        await AlertCoreService.require_checks(
            db, data.check_ids, actor_is_admin=role == UserRole.ADMIN
        )
        payload = data.model_dump()
        payload["notification_provider_ids"] = list(
            dict.fromkeys(data.notification_provider_ids)
        )
        payload["check_ids"] = list(dict.fromkeys(data.check_ids))
        alert = await AlertCoreService.create_alert_response(
            db, AdminAlertCreate(**payload)
        )
        return await AdminAlertCoreService.get_alert(db, alert.id)

    @staticmethod
    async def update_alert(
        db: AsyncSession, alert_id: UUID, data: AdminAlertUpdate
    ) -> AdminAlertDetail:
        if data.trigger_config is not None and data.trigger_type is None:
            # A new config alone is held to the trigger the alert already has.
            alert = await AlertCoreService.get_alert_by_id(db, alert_id)
            try:
                config = normalized_trigger_config(
                    alert.trigger_type, data.trigger_config
                )
            except ValueError as e:
                raise ValidationException(str(e)) from None
            data = data.model_copy(update={"trigger_config": config})
        await AlertCoreService.update_alert_response(db, alert_id, data)
        return await AdminAlertCoreService.get_alert(db, alert_id)

    @staticmethod
    async def set_enabled(
        db: AsyncSession, alert_id: UUID, *, enabled: bool
    ) -> AdminAlertDetail:
        await AlertCoreService.update_alert_response(
            db, alert_id, AlertUpdate(is_enabled=enabled)
        )
        return await AdminAlertCoreService.get_alert(db, alert_id)

    @staticmethod
    async def delete_alert(db: AsyncSession, alert_id: UUID) -> None:
        """Soft-delete the alert, as the web UI does: it notifies no one again."""
        await AlertCoreService.delete_alert(db, alert_id)

    @staticmethod
    async def set_providers(
        db: AsyncSession, alert_id: UUID, data: AdminAlertProviders
    ) -> AlertMappingChange:
        await AlertCoreService.get_alert_by_id(db, alert_id)
        await AlertCoreService.require_providers(db, data.notification_provider_ids)
        added, removed = await AlertCoreService.set_providers(
            db, alert_id, list(dict.fromkeys(data.notification_provider_ids))
        )
        return AlertMappingChange(added=added, removed=removed)

    @staticmethod
    async def set_checks(
        db: AsyncSession, role: UserRole, alert_id: UUID, data: AdminAlertChecks
    ) -> AlertMappingChange:
        alert = await AlertCoreService.get_alert_by_id(db, alert_id)
        # Refused as global before the checks are judged.
        AlertCoreService.require_check_list(alert)
        _, current = await AlertCoreService.get_mapped_ids(db, alert_id)
        # The checks gained and lost are judged; one kept is not touched.
        changed = list(set(current) ^ set(data.check_ids))
        await AlertCoreService.require_checks(
            db, changed, actor_is_admin=role == UserRole.ADMIN
        )
        added, removed = await AlertCoreService.set_checks(
            db, alert_id, list(dict.fromkeys(data.check_ids))
        )
        return AlertMappingChange(added=added, removed=removed)

    @staticmethod
    async def snooze(
        db: AsyncSession,
        username: str,
        alert_id: UUID,
        check_id: UUID,
        minutes: int,
    ) -> AlertCheckSnooze:
        await AlertCoreService.get_alert_by_id(db, alert_id)
        snooze = await AlertCoreService.snooze_alert_check(
            db, alert_id, check_id, minutes
        )
        logger.info(
            "Alert snoozed through the admin API",
            extra={
                "username": username,
                "alert_id": str(alert_id),
                "check_id": str(check_id),
                "snoozed_until": snooze.snoozed_until.isoformat(),
            },
        )
        return snooze

    @staticmethod
    async def unsnooze(
        db: AsyncSession, username: str, alert_id: UUID, check_id: UUID
    ) -> None:
        await AlertCoreService.get_alert_by_id(db, alert_id)
        await AlertCoreService.unsnooze_alert_check(db, alert_id, check_id)
        logger.info(
            "Alert unsnoozed through the admin API",
            extra={
                "username": username,
                "alert_id": str(alert_id),
                "check_id": str(check_id),
            },
        )
