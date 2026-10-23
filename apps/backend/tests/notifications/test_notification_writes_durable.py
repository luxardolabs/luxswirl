"""Notification provider and log writes survive their owner's commit.

Durability locks for moving NotificationCoreService session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.models.notification_provider_model import NotificationProvider
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderUpdate,
)
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import (
    make_alert,
    make_notification_log,
    make_notification_provider,
)
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _seed(maker, *rows) -> None:
    async with maker() as s:
        for row in rows:
            s.add(row)
            await s.flush()
        await s.commit()


async def _provider(maker, provider_id: UUID) -> NotificationProvider | None:
    async with maker() as other:  # separate connection: only committed rows
        return await other.scalar(
            select(NotificationProvider).where(NotificationProvider.id == provider_id)
        )


async def _log_statuses(maker, provider_id: UUID) -> list[str]:
    async with maker() as other:
        rows = await other.scalars(
            select(NotificationLog.status).where(
                NotificationLog.notification_provider_id == provider_id
            )
        )
        return list(rows)


async def test_create_update_and_hard_delete_provider_are_committed(
    worker_db,
) -> None:
    async with worker_db() as s:
        created = await NotificationCoreService.create_provider(
            s,
            NotificationProviderCreate(
                provider_type="webhook",
                friendly_name="durable-create",
                config={"post_url": "https://example.test/hook"},
            ),
        )
        await s.commit()
    stored = await _provider(worker_db, created.id)
    assert stored is not None and stored.friendly_name == "durable-create"

    async with worker_db() as s:
        await NotificationCoreService.update_provider(
            s, created.id, NotificationProviderUpdate(friendly_name="durable-update")
        )
        await s.commit()
    stored = await _provider(worker_db, created.id)
    assert stored is not None and stored.friendly_name == "durable-update"

    async with worker_db() as s:
        await NotificationCoreService.delete_provider(s, created.id, hard_delete=True)
        await s.commit()
    assert await _provider(worker_db, created.id) is None


def _context() -> NotificationContext:
    return NotificationContext(
        check_name="durable",
        check_type="http",
        target="https://example.test",
        agent_id=None,
        status="error",
        success=False,
        latency_ms=1.0,
        error_message=None,
    )


async def _send(maker, provider_id: UUID, alert_id: UUID) -> None:
    async with maker() as s:
        await NotificationCoreService.send_notifications(
            s,
            [provider_id],
            _context(),
            alert_id=alert_id,
            check_result_id=uuid4(),
            check_result_timestamp=utc_now(),
        )
        await s.commit()


async def test_a_disabled_provider_logs_a_committed_suppression(worker_db) -> None:
    provider = make_notification_provider(is_enabled=False)
    alert = make_alert()
    await _seed(worker_db, provider, alert)

    await _send(worker_db, provider.id, alert.id)

    assert await _log_statuses(worker_db, provider.id) == [
        NotificationStatus.SUPPRESSED.value
    ]


async def test_a_rate_limited_send_logs_a_committed_row(worker_db) -> None:
    provider = make_notification_provider(
        rate_limit_count=1, rate_limit_window_minutes=60
    )
    alert = make_alert()
    prior = make_notification_log(
        alert_id=alert.id,
        notification_provider_id=provider.id,
        check_result_id=uuid4(),
        check_result_timestamp=utc_now(),
    )
    await _seed(worker_db, provider, alert, prior)

    await _send(worker_db, provider.id, alert.id)

    assert sorted(await _log_statuses(worker_db, provider.id)) == sorted(
        [NotificationStatus.SENT.value, NotificationStatus.RATE_LIMITED.value]
    )


async def test_a_delivered_send_logs_a_committed_row(
    worker_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def delivered(self, context) -> bool:
        return True

    monkeypatch.setattr(WebhookNotificationProvider, "send", delivered)
    provider = make_notification_provider()
    alert = make_alert()
    await _seed(worker_db, provider, alert)

    await _send(worker_db, provider.id, alert.id)

    assert await _log_statuses(worker_db, provider.id) == [
        NotificationStatus.SENT.value
    ]
