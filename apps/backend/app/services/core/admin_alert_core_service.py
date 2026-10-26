"""
Admin alert core service - alert rules as the admin API serves them.

Every provider and check an alert is given is proved to exist first, so an
unknown id is a 422 naming it, never a foreign-key failure. A soft-deleted
alert is not found.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
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
from app.schemas.pagination_schema import build_pagination
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.notification_core_service import NotificationCoreService


class AdminAlertCoreService:
    """List, read, create, change and delete alerts, and their mappings."""

    @staticmethod
    async def _require_providers(db: AsyncSession, provider_ids: list[UUID]) -> None:
        found = await NotificationCoreService.existing_provider_ids(db, provider_ids)
        missing = [p for p in dict.fromkeys(provider_ids) if p not in found]
        if missing:
            raise ValidationException(
                f"No notification provider has the id {missing[0]}"
            )

    @staticmethod
    async def _require_checks(db: AsyncSession, check_ids: list[UUID]) -> None:
        found = {c.id for c in await CheckCoreService.get_checks_by_ids(db, check_ids)}
        missing = [c for c in dict.fromkeys(check_ids) if c not in found]
        if missing:
            raise ValidationException(f"No check has the id {missing[0]}")

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
        db: AsyncSession, data: AdminAlertCreate
    ) -> AdminAlertDetail:
        await AdminAlertCoreService._require_providers(
            db, data.notification_provider_ids
        )
        await AdminAlertCoreService._require_checks(db, data.check_ids)
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
        await AdminAlertCoreService._require_providers(
            db, data.notification_provider_ids
        )
        added, removed = await AlertCoreService.set_providers(
            db, alert_id, list(dict.fromkeys(data.notification_provider_ids))
        )
        return AlertMappingChange(added=added, removed=removed)

    @staticmethod
    async def set_checks(
        db: AsyncSession, alert_id: UUID, data: AdminAlertChecks
    ) -> AlertMappingChange:
        await AlertCoreService.get_alert_by_id(db, alert_id)
        await AdminAlertCoreService._require_checks(db, data.check_ids)
        added, removed = await AlertCoreService.set_checks(
            db, alert_id, list(dict.fromkeys(data.check_ids))
        )
        return AlertMappingChange(added=added, removed=removed)

    @staticmethod
    async def snooze(
        db: AsyncSession, alert_id: UUID, check_id: UUID, minutes: int
    ) -> AlertCheckSnooze:
        await AlertCoreService.get_alert_by_id(db, alert_id)
        return await AlertCoreService.snooze_alert_check(
            db, alert_id, check_id, minutes
        )

    @staticmethod
    async def unsnooze(db: AsyncSession, alert_id: UUID, check_id: UUID) -> None:
        await AlertCoreService.get_alert_by_id(db, alert_id)
        await AlertCoreService.unsnooze_alert_check(db, alert_id, check_id)
