"""Notification delivery worker — delivers the alert notifications queued in
`notification_logs`, off every request and every transaction that decides them.

Single asyncio task launched in main.py lifespan. It keeps a pool of
`DELIVERY_SLOTS` attempts in flight and claims more as slots free, so one
slow attempt never holds up the rest: rows are claimed FOR UPDATE SKIP LOCKED
in a short transaction and marked `sending` under a lease, each is delivered
with no transaction open, and each outcome is recorded as it comes, in a
short transaction of its own (`NotificationDeliveryCoreService`). No provider
takes more than `PROVIDER_SHARE` of the slots, so one whose every attempt
waits out its timeout leaves the others theirs. Several server processes each
run one; SKIP LOCKED keeps them off each other's rows, and a row whose worker
stopped mid-delivery is claimed again once its lease lapses. Nothing is
reaped at startup: the lease is the only recovery.
"""

import asyncio
import contextlib
from collections import Counter
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
# Attempts in flight at once.
DELIVERY_SLOTS = 20
# The most of them one provider may hold.
PROVIDER_SHARE = 10
_POLL_IDLE_SECONDS = 1.0
_POLL_ERROR_BACKOFF_SECONDS = 5.0


class DeliveryPool:
    """The attempts in flight, claimed as slots free and recorded as they end.

    Claims and records run one at a time on the caller's task, each in a
    transaction `open_session` opens; only the attempts run side by side,
    and they touch no session.
    """

    def __init__(
        self, open_session: SessionOpener, *, now: datetime | None = None
    ) -> None:
        self._open_session = open_session
        self._now = now
        self._in_flight: dict[asyncio.Task[DeliveryOutcome], ClaimedDelivery] = {}
        self.attempts = 0

    @property
    def busy(self) -> bool:
        """Whether any attempt is in flight."""
        return bool(self._in_flight)

    def _clock(self) -> datetime:
        return self._now or utc_now()

    async def fill(self) -> int:
        """Claim due rows for the free slots, within each provider's share,
        and start their attempts; how many rows the claim took, those it
        ended without an attempt included (each may free the next row of its
        alert, subject and provider)."""
        free = DELIVERY_SLOTS - len(self._in_flight)
        if free <= 0:
            return 0
        held = Counter(claimed.provider_id for claimed in self._in_flight.values())
        room = {provider: PROVIDER_SHARE - n for provider, n in held.items()}
        async with self._open_session() as db:
            batch = await NotificationDeliveryCoreService.claim_due(
                db,
                self._clock(),
                free,
                provider_room=room,
                provider_share=PROVIDER_SHARE,
            )
        for claimed in batch.deliveries:
            attempt = asyncio.create_task(
                NotificationDeliveryCoreService.deliver(claimed, batch.network)
            )
            self._in_flight[attempt] = claimed
        self.attempts += len(batch.deliveries)
        return len(batch.deliveries) + batch.ended

    async def settle(self, timeout: float | None) -> None:
        """Wait up to `timeout` (None: no limit) for an attempt to end, and
        record every attempt that has ended."""
        if not self._in_flight:
            return
        ended, _ = await asyncio.wait(
            self._in_flight, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for attempt in ended:
            claimed = self._in_flight.pop(attempt)
            outcome = attempt.result()
            async with self._open_session() as db:
                await NotificationDeliveryCoreService.record(
                    db, claimed, outcome, self._clock()
                )

    async def cancel(self) -> None:
        """Stop every attempt in flight; their rows are claimed again once
        their leases lapse."""
        for attempt in self._in_flight:
            attempt.cancel()
        await asyncio.gather(*self._in_flight, return_exceptions=True)
        self._in_flight.clear()


async def deliver_due_notifications(
    open_session: SessionOpener = worker_session,
    *,
    now: datetime | None = None,
) -> int:
    """Deliver until nothing is due and nothing is in flight: the pool the
    worker runs, run to idle.

    Args:
        open_session: Opens each claim's and each record's transaction.
        now: The instant due rows, leases and retries are judged by (the
            clock, by default).

    Returns:
        How many attempts were made.
    """
    pool = DeliveryPool(open_session, now=now)
    try:
        while True:
            taken = await pool.fill()
            if pool.busy:
                await pool.settle(timeout=None)
            elif not taken:
                return pool.attempts
    finally:
        await pool.cancel()


async def _notification_delivery_loop() -> None:
    """Main worker loop. Runs until cancelled."""
    logger.info("Notification delivery worker started")
    pool = DeliveryPool(worker_session)
    try:
        while True:
            try:
                taken = await pool.fill()
                if pool.busy:
                    # Wakes for an attempt that ends, or to claim rows that
                    # came due meanwhile.
                    await pool.settle(timeout=_POLL_IDLE_SECONDS)
                elif not taken:
                    await asyncio.sleep(_POLL_IDLE_SECONDS)
            except asyncio.CancelledError:
                logger.info("Notification delivery worker cancelled")
                break
            except Exception:
                logger.exception("Notification delivery pass crashed")
                await asyncio.sleep(_POLL_ERROR_BACKOFF_SECONDS)
    finally:
        await pool.cancel()


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
