"""Notification delivery worker — delivers the alert notifications queued in
`notification_logs`, off every request and every transaction that decides them.

Single asyncio task launched in main.py lifespan. Each pass claims due rows
FOR UPDATE SKIP LOCKED in a short transaction and marks them `sending` under a
lease, delivers each with no transaction open, then records each outcome in a
short transaction of its own (`NotificationDeliveryCoreService`). Several
server processes each run one; SKIP LOCKED keeps them off each other's rows,
and a row whose worker stopped mid-delivery is claimed again once its lease
lapses. Nothing is reaped at startup: the lease is the only recovery.
"""

import asyncio
import contextlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.db import worker_session
from app.schemas.notification_delivery_schema import ClaimedDelivery, DeliveryOutcome
from app.services.core.notification_delivery_core_service import (
    NotificationDeliveryCoreService,
)
from app.shared.logger import get_logger

logger = get_logger("luxswirl.background.notification_delivery_worker")

# Opens one transaction: committed on a clean exit, rolled back on an error.
SessionOpener = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_worker_task: asyncio.Task[None] | None = None
# Rows claimed per pass, delivered at once.
DELIVERY_BATCH = 20
_POLL_IDLE_SECONDS = 1.0
_POLL_ERROR_BACKOFF_SECONDS = 5.0


async def deliver_due_notifications(
    open_session: SessionOpener = worker_session,
    *,
    now: datetime | None = None,
    limit: int = DELIVERY_BATCH,
) -> int:
    """One pass: claim the due rows, deliver them, record each outcome.

    Args:
        open_session: Opens each of the pass's transactions.
        now: The instant the pass judges due rows, leases and retries by
            (the clock, by default).
        limit: The most rows claimed.

    Returns:
        How many rows were claimed for an attempt.
    """
    claimed_at = now or utc_now()
    async with open_session() as db:
        batch = await NotificationDeliveryCoreService.claim_due(db, claimed_at, limit)

    async def attempt(
        claimed: ClaimedDelivery,
    ) -> tuple[ClaimedDelivery, DeliveryOutcome]:
        outcome = await NotificationDeliveryCoreService.deliver(claimed, batch.network)
        return claimed, outcome

    # The attempts run side by side and touch no session; each outcome is
    # recorded as it comes, one at a time, in a transaction of its own.
    for finished in asyncio.as_completed(
        [attempt(claimed) for claimed in batch.deliveries]
    ):
        claimed, outcome = await finished
        async with open_session() as db:
            await NotificationDeliveryCoreService.record(
                db, claimed, outcome, now or utc_now()
            )
    return len(batch.deliveries)


async def _notification_delivery_loop() -> None:
    """Main worker loop. Runs until cancelled."""
    logger.info("Notification delivery worker started")
    while True:
        try:
            if not await deliver_due_notifications():
                await asyncio.sleep(_POLL_IDLE_SECONDS)
        except asyncio.CancelledError:
            logger.info("Notification delivery worker cancelled")
            break
        except Exception:
            logger.exception("Notification delivery pass crashed")
            await asyncio.sleep(_POLL_ERROR_BACKOFF_SECONDS)


def start_notification_delivery_worker() -> None:
    """Start the loop (once)."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        logger.warning("Notification delivery worker already running")
        return
    _worker_task = asyncio.create_task(
        _notification_delivery_loop(), name="notification_delivery_worker"
    )


async def stop_notification_delivery_worker() -> None:
    """Stop the loop. A delivery cut off here is claimed again when its lease
    lapses, by this server once restarted or by another."""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _worker_task
    _worker_task = None
    logger.info("Notification delivery worker stopped")
