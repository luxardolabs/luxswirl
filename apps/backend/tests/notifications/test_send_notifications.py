"""NotificationCoreService.send_notifications: one attempt row per provider,
in the order asked, and a provider that is not live refuses the send."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException
from app.models.enum_model import NotificationStatus
from app.notifications.providers.base import NotificationContext
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_alert, make_notification_provider
from sqlalchemy.ext.asyncio import AsyncSession

_CONTEXT = NotificationContext(
    check_name="web",
    check_type="http",
    target="https://example.test",
    agent_id=None,
    status="error",
)


async def _send_ok(self, context: NotificationContext) -> bool:
    return True


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
        rows = await NotificationCoreService.send_notifications(
            db,
            [disabled.id, enabled.id],
            _CONTEXT,
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )

    assert [(r.notification_provider_id, r.status) for r in rows] == [
        (disabled.id, NotificationStatus.SUPPRESSED),
        (enabled.id, NotificationStatus.SENT),
    ]


async def test_a_deleted_provider_refuses_the_send(db: AsyncSession) -> None:
    alert = make_alert()
    live = make_notification_provider()
    gone = make_notification_provider()
    gone.deleted_at = utc_now()
    db.add_all([alert, live, gone])
    await db.flush()

    with pytest.raises(NotFoundException):
        await NotificationCoreService.send_notifications(
            db,
            [live.id, gone.id],
            _CONTEXT,
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )
