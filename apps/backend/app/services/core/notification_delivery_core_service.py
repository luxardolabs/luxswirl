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
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.alert_crud import AlertCRUD
from app.crud.check_crud import CheckCRUD
from app.crud.notification_log_crud import NotificationLogCRUD
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
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
    async def claim_due(
        db: AsyncSession,
        now: datetime,
        limit: int,
        *,
        open_limit: int | None = None,
        provider_share: int | None = None,
    ) -> ClaimedBatch:
        """Claim up to `limit` due rows (`NotificationLogCRUD.claim_due`),
        taken in turn across providers, with at most `provider_share` (all of
        `limit`, by default) in flight for one provider across every server
        process, and past the first `open_limit` (all of `limit`, by default)
        only a provider with nothing in flight given one.

        A row whose payload this release cannot read is failed, one that is
        no longer to be sent (`_refusal`) is recorded as suppressed, and one
        past its provider's rate limit as rate limited, here and now, each on
        its own; the rest are marked `sending` under a lease of `CLAIM_LEASE`
        and returned. The caller commits before delivering.
        """
        rows = await NotificationLogCRUD.claim_due(
            db,
            now,
            limit,
            open_limit=limit if open_limit is None else open_limit,
            provider_share=provider_share or limit,
        )
        deliveries: list[ClaimedDelivery] = []
        ended = 0
        for row in rows:
            context, refusal = NotificationDeliveryCoreService._read(row)
            if refusal is None:
                refusal = await NotificationDeliveryCoreService._refusal(db, row, now)
            if context is not None and refusal is None:
                deliveries.append(
                    await NotificationDeliveryCoreService._claim(db, row, context, now)
                )
                continue
            if refusal is not None:
                status, reason = refusal
                NotificationDeliveryCoreService._end(row, status, reason, now)
                await NotificationLogCRUD.save_logs(db)
                ended += 1
                logger.info(
                    "Notification not delivered",
                    extra={
                        "notification_log_id": str(row.id),
                        "status": status.value,
                        "reason": reason,
                    },
                )
        network = await SettingsCoreService.get_network_protection(db)
        return ClaimedBatch(deliveries=deliveries, network=network, ended=ended)

    @staticmethod
    def _read(
        row: NotificationLog,
    ) -> tuple[NotificationContext | None, tuple[NotificationStatus, str] | None]:
        """The notification a row carries, or why it cannot be delivered: no
        payload, or one this release cannot read (written by another
        release, say), which fails that row alone."""
        if row.payload is None:
            return None, (NotificationStatus.FAILED, "Nothing to deliver")
        try:
            return notification_from_payload(row.payload), None
        except ValidationError:
            logger.warning(
                "Queued notification payload cannot be read",
                extra={"notification_log_id": str(row.id)},
            )
            return None, (NotificationStatus.FAILED, "Unreadable payload")

    @staticmethod
    async def _claim(
        db: AsyncSession,
        row: NotificationLog,
        context: NotificationContext,
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
            context=context,
        )

    @staticmethod
    async def _refusal(
        db: AsyncSession, row: NotificationLog, now: datetime
    ) -> tuple[NotificationStatus, str] | None:
        """Why this row is not to be delivered now, or None to deliver it.

        Read as the alert stands at delivery: a notification whose alert,
        provider or check was disabled, deleted, taken off the alert or
        snoozed since it was decided is suppressed.
        """
        alert = row.alert
        if alert.deleted_at is not None or not alert.is_enabled:
            return NotificationStatus.SUPPRESSED, "Alert is disabled"
        provider = row.notification_provider
        if provider.deleted_at is not None:
            return NotificationStatus.SUPPRESSED, "Notification provider is deleted"
        if not provider.is_enabled:
            # Intentionally disabled — distinct from a delivery failure.
            return NotificationStatus.SUPPRESSED, "Notification provider is disabled"
        if not await AlertCRUD.provider_enabled_on_alert(db, alert.id, provider.id):
            return (
                NotificationStatus.SUPPRESSED,
                "Notification provider is not on the alert",
            )
        if row.check_id is not None:
            check_refusal = await NotificationDeliveryCoreService._check_refusal(
                db, alert.id, row.check_id, now
            )
            if check_refusal is not None:
                return NotificationStatus.SUPPRESSED, check_refusal
        limited, reason = await NotificationCoreService.check_rate_limit(
            db, provider, now=now, log_id=row.id
        )
        if limited:
            return NotificationStatus.RATE_LIMITED, reason or "Rate limit exceeded"
        return None

    @staticmethod
    async def _check_refusal(
        db: AsyncSession, alert_id: UUID, check_id: UUID, now: datetime
    ) -> str | None:
        """Why a notification about this check is not to be sent for this
        alert now: the check deleted, off the alert (or its mapping
        disabled), or the pair snoozed. None to send it."""
        if not await CheckCRUD.exists(db, check_id):
            return "Check is deleted"
        mapping = await AlertCRUD.get_check_mapping(db, alert_id, check_id)
        if mapping is None or not mapping.is_enabled:
            return "Check is not on the alert"
        if mapping.snoozed_until is not None and mapping.snoozed_until > now:
            return "Alert is snoozed for the check"
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
