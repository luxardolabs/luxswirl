"""NotificationCoreService.queue_notifications: one pending row per provider,
in the order asked, each delivered by the delivery worker; a provider that is
not live when its row is delivered is recorded as suppressed, not sent."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.core.datetime_utils import utc_now
from app.models.enum_model import NotificationStatus
from app.notifications.providers.base import NotificationContext
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_alert, make_notification_provider
from fixtures.outbox import deliver_in, queue_and_deliver
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_CONTEXT = NotificationContext(
    check_name="web",
    check_type="http",
    target="https://example.test",
    agent_id=None,
    status="error",
)


async def _send_ok(self, context: NotificationContext) -> bool:
    return True


async def test_queueing_sends_nothing_and_writes_pending_rows(
    db: AsyncSession,
) -> None:
    alert = make_alert()
    first = make_notification_provider()
    second = make_notification_provider()
    db.add_all([alert, first, second])
    await db.flush()
    sent: list[NotificationContext] = []

    async def _record(self, context: NotificationContext) -> bool:
        sent.append(context)
        return True

    with patch(
        "app.notifications.providers.webhook.WebhookNotificationProvider.send",
        _record,
    ):
        rows = await NotificationCoreService.queue_notifications(
            db,
            [second.id, first.id],
            _CONTEXT,
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )

    assert sent == []
    assert [(r.notification_provider_id, r.status) for r in rows] == [
        (second.id, NotificationStatus.PENDING),
        (first.id, NotificationStatus.PENDING),
    ]
    assert rows[0].payload is not None and rows[0].payload["check_name"] == "web"
    assert rows[0].decision_seq < rows[1].decision_seq


async def test_each_provider_gets_its_row_in_the_order_asked(db: AsyncSession) -> None:
    alert = make_alert()
    enabled = make_notification_provider()
    disabled = make_notification_provider(is_enabled=False)
    db.add_all([alert, enabled, disabled])
    await db.flush()

    with patch(
        "app.notifications.providers.webhook.WebhookNotificationProvider.send",
        _send_ok,
    ):
        rows = await queue_and_deliver(
            db, [disabled.id, enabled.id], _CONTEXT, alert_id=alert.id
        )

    assert [(r.notification_provider_id, r.status) for r in rows] == [
        (disabled.id, NotificationStatus.SUPPRESSED),
        (enabled.id, NotificationStatus.SENT),
    ]
    # Delivery ended: the payload is not kept.
    assert [r.payload for r in rows] == [None, None]
    assert all(r.delivered_at is not None for r in rows)


async def test_a_provider_deleted_before_delivery_is_suppressed(
    db: AsyncSession,
) -> None:
    alert = make_alert()
    live = make_notification_provider()
    gone = make_notification_provider()
    db.add_all([alert, live, gone])
    await db.flush()
    rows = await NotificationCoreService.queue_notifications(
        db,
        [live.id, gone.id],
        _CONTEXT,
        alert_id=alert.id,
        check_result_id=None,
        check_result_timestamp=None,
    )
    gone.deleted_at = utc_now()
    await db.flush()

    with patch(
        "app.notifications.providers.webhook.WebhookNotificationProvider.send",
        _send_ok,
    ):
        assert await deliver_in(db) == 1
    for row in rows:
        await db.refresh(row)

    assert [(r.status, r.error_message) for r in rows] == [
        (NotificationStatus.SENT, None),
        (NotificationStatus.SUPPRESSED, "Notification provider is deleted"),
    ]
