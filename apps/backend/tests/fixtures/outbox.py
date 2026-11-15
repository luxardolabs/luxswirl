"""Run the notification delivery worker's passes inside a test.

Alert evaluation only queues notifications (`pending` rows of
`notification_logs`); the delivery worker sends them. No worker runs in the
suite (the app's lifespan does not), so a test that wants what was sent
drives the worker's own pass, `deliver_due_notifications`, until nothing is
due.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from app.background.notification_delivery_worker import deliver_due_notifications
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.services.core.notification_core_service import NotificationCoreService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def deliver_in(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Deliver everything due, every pass on the test's own session `db`
    (rolled back with the test). How many attempts were made."""

    @asynccontextmanager
    async def same_session() -> AsyncIterator[AsyncSession]:
        yield db

    made = 0
    while passed := await deliver_due_notifications(same_session, now=now):
        made += passed
    await db.flush()
    return made


async def deliver_committed(*, now: datetime | None = None) -> int:
    """Deliver everything due, as the worker does: each pass's transactions
    committed for real (with `worker_db`)."""
    made = 0
    while passed := await deliver_due_notifications(now=now):
        made += passed
    return made


async def queue_and_deliver(
    db: AsyncSession,
    provider_ids: Sequence[UUID],
    context: NotificationContext,
    *,
    alert_id: UUID,
) -> list[NotificationLog]:
    """Queue one notification for these providers, deliver it once, and read
    back each provider's row, in `provider_ids` order."""
    queued = await NotificationCoreService.queue_notifications(
        db,
        provider_ids,
        context,
        alert_id=alert_id,
        check_result_id=None,
        check_result_timestamp=None,
    )
    await deliver_in(db)
    ids = [row.id for row in queued]
    result = await db.execute(
        select(NotificationLog)
        .where(NotificationLog.id.in_(ids))
        .execution_options(populate_existing=True)
    )
    by_id = {row.id: row for row in result.scalars().all()}
    return [by_id[log_id] for log_id in ids]
