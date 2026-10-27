"""
Admin notification core service - providers and notification logs as the
admin API serves them.

A viewer reads a provider without its config: the config holds its
credentials, and a webhook URL's path can be one, so it is not masked field by
field but left out. A notification log row's error has each URL cut to its
host, and its check target is shown with any credential masked, as the web
UI's log page shows them to anyone signed in.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_credentials, mask_url_paths
from app.models.enum_model import NotificationStatus, UserRole
from app.schemas.admin_notification_schema import (
    AdminNotificationLog,
    AdminProvider,
    AdminProviderCreate,
    AdminProviderUpdate,
    NotificationLogPage,
    ProviderPage,
    ProviderTestResult,
    ProviderTypeInfo,
)
from app.schemas.notification_log_schema import NotificationLogEntry
from app.schemas.notification_provider_schema import (
    NotificationProviderInDB,
    NotificationProviderUpdate,
)
from app.schemas.pagination_schema import build_pagination
from app.services.core.notification_core_service import NotificationCoreService
from app.services.core.notification_log_core_service import NotificationLogCoreService


class AdminNotificationCoreService:
    """Notification providers and the log of notification attempts."""

    @staticmethod
    def _provider(provider: NotificationProviderInDB, role: UserRole) -> AdminProvider:
        return AdminProvider(
            id=provider.id,
            provider_type=provider.provider_type,
            friendly_name=provider.friendly_name,
            is_enabled=provider.is_enabled,
            is_default_enabled=provider.is_default_enabled,
            rate_limit_count=provider.rate_limit_count,
            rate_limit_window_minutes=provider.rate_limit_window_minutes,
            config=None if role == UserRole.VIEWER else provider.config,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )

    @staticmethod
    async def list_providers(
        db: AsyncSession,
        role: UserRole,
        *,
        page: int,
        per_page: int,
        provider_type: str | None = None,
    ) -> ProviderPage:
        page = max(1, page)
        providers, total = await NotificationCoreService.list_providers(
            db,
            skip=(page - 1) * per_page,
            limit=per_page,
            provider_type=provider_type,
        )
        return ProviderPage.of(
            [AdminNotificationCoreService._provider(p, role) for p in providers],
            build_pagination(page=page, per_page=per_page, total=total),
        )

    @staticmethod
    async def get_provider(
        db: AsyncSession, role: UserRole, provider_id: UUID
    ) -> AdminProvider:
        provider = await NotificationCoreService.get_provider(db, provider_id)
        return AdminNotificationCoreService._provider(provider, role)

    @staticmethod
    def provider_types() -> list[ProviderTypeInfo]:
        """Every provider type, with the config fields it takes."""
        return [
            ProviderTypeInfo(
                type=info["type"],
                name=info["name"],
                description=info["description"],
                config_schema=info["schema"],
            )
            for info in NotificationCoreService.get_available_provider_types()
        ]

    @staticmethod
    async def create_provider(
        db: AsyncSession, role: UserRole, data: AdminProviderCreate
    ) -> AdminProvider:
        provider = await NotificationCoreService.create_provider(db, data)
        return AdminNotificationCoreService._provider(provider, role)

    @staticmethod
    async def update_provider(
        db: AsyncSession,
        role: UserRole,
        provider_id: UUID,
        data: AdminProviderUpdate | NotificationProviderUpdate,
    ) -> AdminProvider:
        provider = await NotificationCoreService.update_provider(db, provider_id, data)
        return AdminNotificationCoreService._provider(provider, role)

    @staticmethod
    async def set_enabled(
        db: AsyncSession, role: UserRole, provider_id: UUID, *, enabled: bool
    ) -> AdminProvider:
        return await AdminNotificationCoreService.update_provider(
            db, role, provider_id, NotificationProviderUpdate(is_enabled=enabled)
        )

    @staticmethod
    async def delete_provider(db: AsyncSession, provider_id: UUID) -> None:
        """Soft-delete the provider, as the web UI does: it is sent nothing again."""
        await NotificationCoreService.delete_provider(db, provider_id)

    @staticmethod
    async def test_provider(db: AsyncSession, provider_id: UUID) -> ProviderTestResult:
        """Send the provider a test notification; its outcome, no URL in the error."""
        success, error = await NotificationCoreService.test_provider(db, provider_id)
        return ProviderTestResult(success=success, error=mask_url_paths(error))

    @staticmethod
    def _log(entry: NotificationLogEntry) -> AdminNotificationLog:
        return AdminNotificationLog(
            id=entry.id,
            sent_at=entry.sent_at,
            status=entry.status,
            error_message=mask_url_paths(entry.error_message),
            alert_id=entry.alert_id,
            alert_name=entry.alert_name,
            provider_name=entry.provider_name,
            check_id=entry.check_id,
            check_name=entry.display_name,
            check_type=entry.check_type,
            check_target=mask_credentials(entry.target),
            snoozed_until=entry.snoozed_until,
        )

    @staticmethod
    async def list_logs(
        db: AsyncSession,
        *,
        page: int,
        per_page: int,
        status: NotificationStatus | None = None,
        alert_id: UUID | None = None,
        notification_provider_id: UUID | None = None,
        search: str | None = None,
    ) -> NotificationLogPage:
        """One page of notification attempts, newest first."""
        page = max(1, page)
        entries, total = await NotificationLogCoreService.get_logs_paginated(
            db,
            status=status.value if status else None,
            alert_id=alert_id,
            notification_provider_id=notification_provider_id,
            search=search,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        return NotificationLogPage.of(
            [AdminNotificationCoreService._log(e) for e in entries],
            build_pagination(page=page, per_page=per_page, total=total),
        )
