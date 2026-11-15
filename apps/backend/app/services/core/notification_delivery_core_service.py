"""
Notification delivery - the outbox side of alert notifications.

Alert evaluation decides a notification and writes it as a `pending` row of
`notification_logs` (`NotificationCoreService.queue_notifications`). The
delivery worker (`app/background/notification_delivery_worker.py`) drives this
service: it claims due rows in a short transaction (`claim_due`), delivers
each outside any transaction (`deliver`), then records the outcome in another
short transaction (`record`). No database lock is held while a provider is
waited on.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.notification_log_crud import NotificationLogCRUD
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.schemas.notification_delivery_schema import (
    ClaimedBatch,
    ClaimedDelivery,
    DeliveryOutcome,
)
from app.schemas.setting_schema import NetworkProtection
from app.services.core.notification_core_service import (
    NotificationCoreService,
    notification_from_payload,
)
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.notification_delivery")

# The longest one attempt may take, whatever timeout a provider's settings
# give it: past this the attempt counts as failed.
ATTEMPT_TIMEOUT_SECONDS = 120.0

# How long a claim holds a row. Longer than an attempt can take, so a row is
# claimed again only when its worker stopped (a crash, a restart) before
# recording the outcome; it is then delivered again.
CLAIM_LEASE = timedelta(seconds=ATTEMPT_TIMEOUT_SECONDS + 60)

# Attempts per notification, and the wait before each attempt after the first.
MAX_ATTEMPTS = 5
RETRY_DELAYS = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
)


class NotificationDeliveryCoreService:
    """Claims, delivers and records queued alert notifications."""

    @staticmethod
    async def claim_due(db: AsyncSession, now: datetime, limit: int) -> ClaimedBatch:
        """Claim up to `limit` due rows (`NotificationLogCRUD.claim_due`).

        A row whose alert is disabled or deleted, or whose provider is
        disabled or deleted, is recorded as suppressed, and one past its
        provider's rate limit as rate limited, here and now; the rest are
        marked `sending` under a lease of `CLAIM_LEASE` and returned. The
        caller commits before delivering.
        """
        rows = await NotificationLogCRUD.claim_due(db, now, limit)
        deliveries: list[ClaimedDelivery] = []
        for row in rows:
            payload = row.payload
            if payload is None:
                refusal: tuple[NotificationStatus, str] | None = (
                    NotificationStatus.FAILED,
                    "Nothing to deliver",
                )
            else:
                refusal = await NotificationDeliveryCoreService._refusal(db, row)
            if payload is not None and refusal is None:
                deliveries.append(
                    await NotificationDeliveryCoreService._claim(db, row, payload, now)
                )
                continue
            if refusal is not None:
                status, reason = refusal
                NotificationDeliveryCoreService._end(row, status, reason, now)
                await NotificationLogCRUD.save_logs(db)
                logger.info(
                    "Notification not delivered",
                    extra={
                        "notification_log_id": str(row.id),
                        "status": status.value,
                        "reason": reason,
                    },
                )
        network = await SettingsCoreService.get_network_protection(db)
        return ClaimedBatch(deliveries=deliveries, network=network)

    @staticmethod
    async def _claim(
        db: AsyncSession,
        row: NotificationLog,
        payload: dict[str, Any],
        now: datetime,
    ) -> ClaimedDelivery:
        """Mark a row `sending` for one more attempt, under a lease."""
        attempt = await NotificationLogCRUD.mark_sending(
            db, row.id, lease_expires_at=now + CLAIM_LEASE
        )
        provider = row.notification_provider
        return ClaimedDelivery(
            log_id=row.id,
            attempt=attempt,
            provider_id=provider.id,
            provider_type=provider.provider_type,
            provider_config=dict(provider.config),
            context=notification_from_payload(payload),
        )

    @staticmethod
    async def _refusal(
        db: AsyncSession, row: NotificationLog
    ) -> tuple[NotificationStatus, str] | None:
        """Why this row is not to be delivered now, or None to deliver it."""
        alert = row.alert
        if alert.deleted_at is not None or not alert.is_enabled:
            return NotificationStatus.SUPPRESSED, "Alert is disabled"
        provider = row.notification_provider
        if provider.deleted_at is not None:
            return NotificationStatus.SUPPRESSED, "Notification provider is deleted"
        if not provider.is_enabled:
            # Intentionally disabled — distinct from a delivery failure.
            return NotificationStatus.SUPPRESSED, "Notification provider is disabled"
        limited, reason = await NotificationCoreService.check_rate_limit(db, provider)
        if limited:
            return NotificationStatus.RATE_LIMITED, reason or "Rate limit exceeded"
        return None

    @staticmethod
    def _end(
        row: NotificationLog,
        status: NotificationStatus,
        error: str | None,
        now: datetime,
    ) -> None:
        """Close a claimed row's delivery with `status`."""
        row.status = status
        row.error_message = error
        row.delivered_at = now
        row.payload = None
        row.next_attempt_at = None
        row.lease_expires_at = None

    @staticmethod
    async def deliver(
        claimed: ClaimedDelivery, network: NetworkProtection
    ) -> DeliveryOutcome:
        """One attempt at a claimed row, with no database work."""
        return await NotificationCoreService.deliver(
            claimed.provider_type,
            claimed.provider_id,
            claimed.provider_config,
            claimed.context,
            network,
            timeout_seconds=ATTEMPT_TIMEOUT_SECONDS,
        )

    @staticmethod
    async def record(
        db: AsyncSession,
        claimed: ClaimedDelivery,
        outcome: DeliveryOutcome,
        now: datetime,
    ) -> bool:
        """Record an attempt's outcome.

        Sent, or failed for good (a refusal that would repeat, or the last of
        `MAX_ATTEMPTS`), ends the row's delivery; any other failure leaves it
        `retrying`, due again after the next of `RETRY_DELAYS`, its error
        shown meanwhile. False when the row is no longer this attempt's (see
        `NotificationLogCRUD.finish_attempt`).
        """
        done: dict[str, Any] = {
            "delivered_at": now,
            "payload": None,
            "next_attempt_at": None,
            "lease_expires_at": None,
        }
        if outcome.status == NotificationStatus.SENT:
            values = {**done, "status": NotificationStatus.SENT, "error_message": None}
        elif outcome.retryable and claimed.attempt < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[min(claimed.attempt, len(RETRY_DELAYS)) - 1]
            values = {
                "status": NotificationStatus.RETRYING,
                "error_message": outcome.error,
                "next_attempt_at": now + delay,
                "lease_expires_at": None,
            }
        else:
            values = {
                **done,
                "status": NotificationStatus.FAILED,
                "error_message": outcome.error,
            }
        recorded = await NotificationLogCRUD.finish_attempt(
            db, claimed.log_id, claimed.attempt, values
        )
        if not recorded:
            logger.warning(
                "Notification outcome not recorded: the row was deleted or claimed again",
                extra={
                    "notification_log_id": str(claimed.log_id),
                    "attempt": claimed.attempt,
                },
            )
        return recorded
