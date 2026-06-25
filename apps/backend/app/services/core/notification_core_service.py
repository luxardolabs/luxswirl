"""
Notification service - business logic for notification provider operations.
"""

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException, ValidationException
from app.crud.notification_log_crud import NotificationLogCRUD
from app.crud.notification_provider_crud import NotificationProviderCRUD
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.models.notification_provider_model import NotificationProvider
from app.notifications.providers.base import NotificationContext
from app.notifications.registry import NotificationRegistry
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)

logger = get_logger("luxswirl.services.notification")


class NotificationCoreService:
    """Service for notification provider operations."""

    @staticmethod
    async def get_provider_by_id(
        db: AsyncSession,
        provider_id: UUID,
        include_deleted: bool = False,
    ) -> NotificationProvider:
        """
        Get notification provider by ID.

        Args:
            db: Database session
            provider_id: Provider database ID
            include_deleted: Whether to include soft-deleted providers

        Returns:
            NotificationProvider instance

        Raises:
            NotFoundException: If provider not found
        """
        provider = await NotificationProviderCRUD.get_by_id(
            db, provider_id, include_deleted=include_deleted
        )

        if not provider:
            raise NotFoundException(f"Notification provider {provider_id} not found")

        return provider

    @staticmethod
    async def list_providers(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        provider_type: str | None = None,
        include_deleted: bool = False,
    ) -> tuple[Sequence[NotificationProvider], int]:
        """
        List notification providers with pagination.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            provider_type: Filter by provider type
            include_deleted: Whether to include soft-deleted providers

        Returns:
            Tuple of (providers list, total count)
        """
        return await NotificationProviderCRUD.list_paginated(
            db,
            skip=skip,
            limit=limit,
            provider_type=provider_type,
            include_deleted=include_deleted,
        )

    @staticmethod
    async def create_provider(
        db: AsyncSession,
        data: NotificationProviderCreate,
    ) -> NotificationProvider:
        """
        Create a new notification provider.

        Args:
            db: Database session
            data: Provider creation data

        Returns:
            Created provider

        Raises:
            ValidationException: If provider type is invalid or config is invalid
        """
        # Validate provider type is registered
        if not NotificationRegistry.is_registered(data.provider_type):
            available = ", ".join(NotificationRegistry.get_provider_types())
            raise ValidationException(
                f"Invalid provider type: {data.provider_type}. Available: {available}"
            )

        # Validate configuration by creating provider instance
        try:
            provider_class = NotificationRegistry.get(data.provider_type)
            if provider_class is None:
                raise ValidationException(f"Unknown provider type: {data.provider_type}")
            provider_class(data.config)  # Validate config by instantiating
        except ValidationException:
            raise  # Re-raise validation exceptions as-is
        except Exception as e:
            raise ValidationException(f"Invalid provider configuration: {str(e)}") from e

        # Create database record
        provider = NotificationProvider(
            provider_type=data.provider_type,
            friendly_name=data.friendly_name,
            config=data.config,
            is_default_enabled=data.is_default_enabled,
            rate_limit_count=data.rate_limit_count,
            rate_limit_window_minutes=data.rate_limit_window_minutes,
        )

        db.add(provider)
        await db.flush()
        await db.refresh(provider)

        logger.info(
            "Created notification provider",
            extra={
                "provider_friendly_name": provider.friendly_name,
                "provider_type": provider.provider_type,
                "provider_id": str(provider.id),
            },
        )

        return provider

    @staticmethod
    async def update_provider(
        db: AsyncSession,
        provider_id: UUID,
        data: NotificationProviderUpdate,
    ) -> NotificationProvider:
        """
        Update a notification provider.

        Args:
            db: Database session
            provider_id: Provider ID
            data: Update data

        Returns:
            Updated provider

        Raises:
            NotFoundException: If provider not found
            ValidationException: If config is invalid
        """
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)

        # Update fields
        if data.friendly_name is not None:
            provider.friendly_name = data.friendly_name

        if data.config is not None:
            # Validate new configuration
            try:
                provider_class = NotificationRegistry.get(provider.provider_type)
                if provider_class is None:
                    raise ValidationException(f"Unknown provider type: {provider.provider_type}")
                provider_class(data.config)  # Validate config by instantiating
            except ValidationException:
                raise  # Re-raise validation exceptions as-is
            except Exception as e:
                raise ValidationException(f"Invalid provider configuration: {str(e)}") from e

            provider.config = data.config

        if data.is_enabled is not None:
            provider.is_enabled = data.is_enabled

        if data.is_default_enabled is not None:
            provider.is_default_enabled = data.is_default_enabled

        if data.rate_limit_count is not None:
            provider.rate_limit_count = data.rate_limit_count

        if data.rate_limit_window_minutes is not None:
            provider.rate_limit_window_minutes = data.rate_limit_window_minutes

        await db.flush()
        await db.refresh(provider)

        logger.info(
            "Updated notification provider",
            extra={"provider_id": str(provider.id)},
        )

        return provider

    @staticmethod
    async def delete_provider(
        db: AsyncSession,
        provider_id: UUID,
        hard_delete: bool = False,
    ) -> None:
        """
        Delete a notification provider (soft delete by default).

        Args:
            db: Database session
            provider_id: Provider ID
            hard_delete: If True, permanently delete; if False, soft delete

        Raises:
            NotFoundException: If provider not found
        """
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)

        if hard_delete:
            await db.delete(provider)
        else:
            provider.deleted_at = utc_now()

        logger.info(
            "Deleted notification provider",
            extra={
                "provider_id": str(provider_id),
                "delete_kind": "hard" if hard_delete else "soft",
            },
        )

    @staticmethod
    async def test_provider(
        db: AsyncSession,
        provider_id: UUID,
        test_message: str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Send a test notification through a provider.

        Args:
            db: Database session
            provider_id: Provider ID
            test_message: Custom test message

        Returns:
            Tuple of (success, error_message)

        Raises:
            NotFoundException: If provider not found
        """
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)

        # Create test notification context
        context = NotificationContext(
            check_name="Test Check",
            check_type="test",
            target="test.example.com",
            agent_id=None,
            agent_name="Test Agent",
            status="success",
            success=True,
            latency_ms=123.45,
            timestamp=utc_now().isoformat(),
            alert_name="Test Alert",
            alert_description="This is a test notification",
            custom_message=test_message,
        )

        try:
            # Create provider instance and send
            provider_instance = NotificationRegistry.create_provider(
                provider.provider_type,
                provider.config,
            )
            success = await provider_instance.send(context)

            if success:
                logger.info(
                    "Test notification sent successfully",
                    extra={"provider_id": str(provider_id)},
                )
                return True, None
            else:
                logger.warning(
                    "Test notification failed",
                    extra={"provider_id": str(provider_id)},
                )
                return False, "Provider returned failure status"

        except Exception as e:
            error_msg = str(e)
            logger.error(
                "Test notification error",
                extra={"provider_id": str(provider_id), "error_message": error_msg},
            )
            return False, error_msg

    @staticmethod
    async def send_notification(
        db: AsyncSession,
        provider_id: UUID,
        context: NotificationContext,
        alert_id: UUID,
        check_result_id: UUID,
        check_result_timestamp,
        check_id: UUID | None = None,
        notification_hash: str | None = None,
        is_resend: bool = False,
        resend_count: int = 0,
    ) -> NotificationLog:
        """
        Send a notification and log the attempt.

        Args:
            db: Database session
            provider_id: Provider ID
            context: Notification context
            alert_id: Alert that triggered this notification
            check_result_id: Check result ID that triggered this
            check_result_timestamp: Check result timestamp
            notification_hash: Per-alert-state key (LUXSWIRL-147) — stored on the row
                so dedup can detect transitions.
            is_resend: True if this is a same-state resend after resend_interval
                elapsed (LUXSWIRL-149). False on fresh fires and state transitions.
            resend_count: Cumulative resend count within the current state. 0 on
                fresh fires; prior+1 on resends.

        Returns:
            NotificationLog record

        Raises:
            NotFoundException: If provider not found
        """
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)

        # Check if provider is enabled
        if not provider.is_enabled:
            # Log as suppressed (provider intentionally disabled — distinct from
            # delivery failure; useful for not setting off alarms in dashboards)
            log_entry = NotificationLog(
                alert_id=alert_id,
                notification_provider_id=provider_id,
                check_result_id=check_result_id,
                check_result_timestamp=check_result_timestamp,
                check_id=check_id,
                check_success=context.success,
                check_latency_ms=context.latency_ms,
                status=NotificationStatus.SUPPRESSED.value,
                error_message="Notification provider is disabled",
                notification_hash=notification_hash,
                is_resend=is_resend,
                resend_count=resend_count,
                sent_at=utc_now(),
            )
            db.add(log_entry)

            logger.info(
                "Notification suppressed - provider is disabled",
                extra={"provider_id": str(provider_id)},
            )
            return log_entry

        # Check rate limiting
        is_rate_limited, reason = await NotificationCoreService._check_rate_limit(db, provider)
        if is_rate_limited:
            # Log as rate limited
            log_entry = NotificationLog(
                alert_id=alert_id,
                notification_provider_id=provider_id,
                check_result_id=check_result_id,
                check_result_timestamp=check_result_timestamp,
                check_id=check_id,
                check_success=context.success,
                check_latency_ms=context.latency_ms,
                status=NotificationStatus.RATE_LIMITED.value,
                error_message=reason,
                notification_hash=notification_hash,
                is_resend=is_resend,
                resend_count=resend_count,
                sent_at=utc_now(),
            )
            db.add(log_entry)

            logger.warning(
                "Notification rate limited",
                extra={"provider_id": str(provider_id), "reason": reason},
            )
            return log_entry

        # Try to send
        try:
            provider_instance = NotificationRegistry.create_provider(
                provider.provider_type,
                provider.config,
            )
            success = await provider_instance.send(context)

            # Log the attempt
            log_entry = NotificationLog(
                alert_id=alert_id,
                notification_provider_id=provider_id,
                check_result_id=check_result_id,
                check_result_timestamp=check_result_timestamp,
                check_id=check_id,
                check_success=context.success,
                check_latency_ms=context.latency_ms,
                status=(
                    NotificationStatus.SENT.value if success else NotificationStatus.FAILED.value
                ),
                error_message=None if success else "Provider returned failure",
                notification_hash=notification_hash,
                is_resend=is_resend,
                resend_count=resend_count,
                sent_at=utc_now(),
            )

        except Exception as e:
            # Log the failure
            error_msg = str(e)
            log_entry = NotificationLog(
                alert_id=alert_id,
                notification_provider_id=provider_id,
                check_result_id=check_result_id,
                check_result_timestamp=check_result_timestamp,
                check_id=check_id,
                check_success=context.success,
                check_latency_ms=context.latency_ms,
                status=NotificationStatus.FAILED.value,
                error_message=error_msg,
                notification_hash=notification_hash,
                is_resend=is_resend,
                resend_count=resend_count,
                sent_at=utc_now(),
            )
            logger.error(
                "Notification send failed",
                extra={"provider_id": str(provider_id), "error_message": error_msg},
            )

        db.add(log_entry)
        await db.flush()
        await db.refresh(log_entry)

        return log_entry

    @staticmethod
    async def _check_rate_limit(
        db: AsyncSession,
        provider: NotificationProvider,
    ) -> tuple[bool, str | None]:
        """
        Check if provider has exceeded rate limit.

        Args:
            db: Database session
            provider: Notification provider

        Returns:
            Tuple of (is_limited, reason)
        """
        if not provider.rate_limit_count or not provider.rate_limit_window_minutes:
            return False, None

        # Count notifications sent in the time window
        window_start = utc_now() - timedelta(minutes=provider.rate_limit_window_minutes)

        count = await NotificationLogCRUD.count_for_provider_since(
            db, provider.id, window_start, ["sent", "rate_limited"]
        )

        if count >= provider.rate_limit_count:
            return True, (
                f"Rate limit exceeded: {count}/{provider.rate_limit_count} "
                f"notifications in {provider.rate_limit_window_minutes} minutes"
            )

        return False, None

    @staticmethod
    def get_available_provider_types() -> list[dict]:
        """
        Get list of available provider types with their schemas.

        Returns:
            List of provider information dictionaries
        """
        return NotificationRegistry.get_provider_info()
