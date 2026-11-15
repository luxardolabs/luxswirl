"""Run the alert decision and notification delivery workers inside a test.

A report only queues its results for their alert decisions; the alert
decision worker decides them (writing `pending` rows of `notification_logs`)
and the delivery worker sends those. No worker runs in the suite (the app's
lifespan does not), so a test that wants what was decided or sent drives the
workers' own passes, `decide_queued_alerts` until nothing is queued, then
`deliver_due_notifications`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from app.background.alert_decision_worker import decide_queued_alerts
from app.background.notification_delivery_worker import deliver_due_notifications
from app.crud.alert_crud import AlertCRUD
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.services.core.notification_core_service import NotificationCoreService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def decide_in(db: AsyncSession) -> int:
    """Decide everything queued, every pass on the test's own session `db`
    (rolled back with the test). How many queued results were taken."""

    @asynccontextmanager
    async def same_session() -> AsyncIterator[AsyncSession]:
        yield db

    taken = 0
    while passed := await decide_queued_alerts(same_session):
        taken += passed
    await db.flush()
    return taken


async def deliver_in(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Decide everything queued, then deliver everything due, on the test's
    own session `db` (rolled back with the test). How many attempts were
    made."""

    @asynccontextmanager
    async def same_session() -> AsyncIterator[AsyncSession]:
        yield db

    await decide_in(db)
    made = await deliver_due_notifications(same_session, now=now)
    await db.flush()
    return made


async def decide_committed() -> int:
    """Decide everything queued, as the alert decision worker does: each
    check's decisions committed for real (with `worker_db`). How many queued
    results were taken."""
    taken = 0
    while passed := await decide_queued_alerts():
        taken += passed
    return taken


async def deliver_committed(*, now: datetime | None = None) -> int:
    """Decide everything queued, then deliver everything due, as the two
    workers do: each transaction committed for real (with `worker_db`). How
    many attempts were made."""
    await decide_committed()
    return await deliver_due_notifications(now=now)


async def attach_providers(
    db: AsyncSession, alert_id: UUID, provider_ids: Sequence[UUID]
) -> None:
    """Map these providers to the alert, as an alert's own providers are: the
    delivery worker suppresses a notification for a provider not on its
    alert."""
    await AlertCRUD.replace_provider_mappings(
        db,
        alert_id,
        [*await AlertCRUD.list_mapped_provider_ids(db, alert_id), *provider_ids],
    )


async def queue_and_deliver(
    db: AsyncSession,
    provider_ids: Sequence[UUID],
    context: NotificationContext,
    *,
    alert_id: UUID,
) -> list[NotificationLog]:
    """Attach these providers to the alert, queue one notification for them,
    deliver it once, and read back each provider's row, in `provider_ids`
    order."""
    await attach_providers(db, alert_id, provider_ids)
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
