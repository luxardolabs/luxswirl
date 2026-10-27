"""
Notification service - business logic for notification provider operations.
"""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_mask import mask_url_paths
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
    NotificationProviderInDB,
    NotificationProviderUpdate,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.notification")


class NotificationCoreService:
    """Service for notification provider operations."""

    @staticmethod
    async def get_provider(
        db: AsyncSession,
        provider_id: UUID,
        include_deleted: bool = False,
    ) -> NotificationProviderInDB:
        """The provider as the web seam holds it — a DTO, not the ORM row.

        Unmasked on purpose: the edit form re-renders the saved secret so an
        operator can save without retyping it.
        """
        provider = await NotificationCoreService.get_provider_by_id(
            db, provider_id, include_deleted
        )
        return NotificationProviderInDB.model_validate(provider)

    @staticmethod
    async def existing_provider_ids(
        db: AsyncSession, provider_ids: list[UUID]
    ) -> set[UUID]:
        """Which of `provider_ids` are live (not deleted) providers."""
        return set(await NotificationProviderCRUD.get_by_ids(db, provider_ids))

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
    ) -> tuple[list[NotificationProviderInDB], int]:
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
        providers, total = await NotificationProviderCRUD.list_paginated(
            db,
            skip=skip,
            limit=limit,
            provider_type=provider_type,
            include_deleted=include_deleted,
        )
        return [NotificationProviderInDB.model_validate(p) for p in providers], total

    @staticmethod
    async def create_provider(
        db: AsyncSession,
        data: NotificationProviderCreate,
    ) -> NotificationProviderInDB:
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
                raise ValidationException(
                    f"Unknown provider type: {data.provider_type}"
                )
            provider_class(data.config)  # Validate config by instantiating
        except ValidationException:
            raise  # Re-raise validation exceptions as-is
        except Exception as e:
            raise ValidationException(
                f"Invalid provider configuration: {str(e)}"
            ) from e

        # Create database record
        provider = NotificationProvider(
            provider_type=data.provider_type,
            friendly_name=data.friendly_name,
            config=data.config,
            is_default_enabled=data.is_default_enabled,
            rate_limit_count=data.rate_limit_count,
            rate_limit_window_minutes=data.rate_limit_window_minutes,
        )

        provider = await NotificationProviderCRUD.add_provider(db, provider)

        logger.info(
            "Created notification provider",
            extra={
                "provider_friendly_name": provider.friendly_name,
                "provider_type": provider.provider_type,
                "provider_id": str(provider.id),
            },
        )

        return NotificationProviderInDB.model_validate(provider)

    @staticmethod
    async def update_provider(
        db: AsyncSession,
        provider_id: UUID,
        data: NotificationProviderUpdate,
    ) -> NotificationProviderInDB:
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
                    raise ValidationException(
                        f"Unknown provider type: {provider.provider_type}"
                    )
                provider_class(data.config)  # Validate config by instantiating
            except ValidationException:
                raise  # Re-raise validation exceptions as-is
            except Exception as e:
                raise ValidationException(
                    f"Invalid provider configuration: {str(e)}"
                ) from e

            provider.config = data.config

        if data.is_enabled is not None:
            provider.is_enabled = data.is_enabled

        if data.is_default_enabled is not None:
            provider.is_default_enabled = data.is_default_enabled

        if data.rate_limit_count is not None:
            provider.rate_limit_count = data.rate_limit_count

        if data.rate_limit_window_minutes is not None:
            provider.rate_limit_window_minutes = data.rate_limit_window_minutes

        provider = await NotificationProviderCRUD.save_provider(db, provider)

        logger.info(
            "Updated notification provider",
            extra={"provider_id": str(provider.id)},
        )

        return NotificationProviderInDB.model_validate(provider)

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
            await NotificationProviderCRUD.hard_delete_provider(db, provider)
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
            error_msg = NotificationCoreService._delivery_error(e)
            logger.exception(
                "Test notification error",
                extra={"provider_id": str(provider_id), "error_message": error_msg},
            )
            return False, error_msg

    @staticmethod
    async def send_notifications(
        db: AsyncSession,
        provider_ids: Sequence[UUID],
        context: NotificationContext,
        *,
        alert_id: UUID,
        check_result_id: UUID | None,
        check_result_timestamp: datetime | None,
        check_id: UUID | None = None,
        agent_id: UUID | None = None,
        notification_hash: str | None = None,
        is_resend: bool = False,
        resend_count: int = 0,
    ) -> list[NotificationLog]:
        """Send a notification through several providers and log each attempt.

        The session is used one statement at a time (an AsyncSession is not
        safe to share between concurrent tasks). The providers are read and
        rate-limit-checked in turn, the deliveries run in parallel with no
        database work, and each attempt's row is written in turn after them.

        Args:
            db: Database session
            provider_ids: The providers to send through
            context: Notification context
            alert_id: Alert that triggered this notification
            check_result_id: Check result ID that triggered this (None when
                missing data triggered it: no_data, agent_offline)
            check_result_timestamp: Check result timestamp (None with it)
            check_id: The check the notification is about
            agent_id: The agent an agent_offline notification is about
            notification_hash: The alert's state key, stored on each row so
                dedup can detect transitions.
            is_resend: True for a same-state resend after the alert's resend
                interval; False on fresh fires and state transitions.
            resend_count: Resends within the current state: 0 on fresh fires.

        Returns:
            One NotificationLog row per provider, in `provider_ids` order.

        Raises:
            NotFoundException: If a provider is not found
        """

        def _row(
            provider_id: UUID, status: NotificationStatus, error: str | None
        ) -> NotificationLog:
            return NotificationLog(
                alert_id=alert_id,
                notification_provider_id=provider_id,
                check_result_id=check_result_id,
                check_result_timestamp=check_result_timestamp,
                check_id=check_id,
                agent_id=agent_id,
                check_success=context.success,
                check_latency_ms=context.latency_ms,
                status=status,
                error_message=error,
                notification_hash=notification_hash,
                is_resend=is_resend,
                resend_count=resend_count,
                sent_at=utc_now(),
            )

        # 1. Gate each provider (database reads, in turn).
        outcomes: dict[UUID, tuple[NotificationStatus, str | None]] = {}
        deliverable: list[NotificationProvider] = []
        providers = await NotificationProviderCRUD.get_by_ids(db, provider_ids)
        for provider_id in provider_ids:
            provider = providers.get(provider_id)
            if provider is None:
                raise NotFoundException(
                    f"Notification provider {provider_id} not found"
                )
            if not provider.is_enabled:
                # Intentionally disabled — distinct from a delivery failure.
                outcomes[provider_id] = (
                    NotificationStatus.SUPPRESSED,
                    "Notification provider is disabled",
                )
                logger.info(
                    "Notification suppressed - provider is disabled",
                    extra={"provider_id": str(provider_id)},
                )
                continue
            is_rate_limited, reason = await NotificationCoreService._check_rate_limit(
                db, provider
            )
            if is_rate_limited:
                outcomes[provider_id] = (NotificationStatus.RATE_LIMITED, reason)
                logger.warning(
                    "Notification rate limited",
                    extra={"provider_id": str(provider_id), "reason": reason},
                )
                continue
            deliverable.append(provider)

        # 2. Deliver in parallel (no database work).
        delivered = await asyncio.gather(
            *(
                NotificationCoreService._deliver(provider, context)
                for provider in deliverable
            )
        )
        for provider, outcome in zip(deliverable, delivered, strict=True):
            outcomes[provider.id] = outcome

        # 3. Record every attempt (database writes, in turn).
        rows: list[NotificationLog] = []
        for provider_id in provider_ids:
            status, error = outcomes[provider_id]
            rows.append(
                await NotificationLogCRUD.add_log(db, _row(provider_id, status, error))
            )
        return rows

    @staticmethod
    def _delivery_error(error: Exception) -> str:
        """A failed delivery's error as it is stored and shown, without the
        endpoint's URL: its path or query can be the provider's credential, and
        any user may read the notification log."""
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            return f"HTTP {response.status_code} {response.reason_phrase}".strip()
        detail = mask_url_paths(str(error)) or ""
        return f"{type(error).__name__}: {detail}" if detail else type(error).__name__

    @staticmethod
    async def _deliver(
        provider: NotificationProvider, context: NotificationContext
    ) -> tuple[NotificationStatus, str | None]:
        """One provider's delivery attempt: its status and error, never raised."""
        try:
            provider_instance = NotificationRegistry.create_provider(
                provider.provider_type,
                provider.config,
            )
            if await provider_instance.send(context):
                return NotificationStatus.SENT, None
            return NotificationStatus.FAILED, "Provider returned failure"
        except Exception as e:
            error_msg = NotificationCoreService._delivery_error(e)
            logger.exception(
                "Notification send failed",
                extra={"provider_id": str(provider.id), "error_message": error_msg},
            )
            return NotificationStatus.FAILED, error_msg

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
    def get_available_provider_types() -> list[dict[str, Any]]:
        """
        Get list of available provider types with their schemas.

        Returns:
            List of provider information dictionaries
        """
        return NotificationRegistry.get_provider_info()
