"""Shared helpers for the notification outbox tests: a webhook that records
what it was sent (or hangs, as a blackholed endpoint does), an alert with its
providers, and notifications queued for it."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_alert, make_alert_notification_mapping


def notification_context(name: str) -> NotificationContext:
    return NotificationContext(
        check_name=name,
        check_type="ping",
        target="192.0.2.1",
        agent_id=None,
        agent_name="a",
        status="error",
        success=False,
        latency_ms=1.0,
        timestamp=utc_now().isoformat(),
        alert_name="al",
    )


class WebhookHooks:
    """Records each send per provider url, with when it was made; a url in
    `slow` waits `delay` seconds and then fails, as a blackholed endpoint
    does."""

    def __init__(self, slow: set[str] | None = None, delay: float = 0.0) -> None:
        self.slow = slow or set()
        self.delay = delay
        self.sent: list[tuple[str, str, float]] = []
        self.entered = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = self

        async def send(self: Any, context: NotificationContext) -> bool:
            url = self.config["post_url"]
            hook.entered.set()
            if url in hook.slow:
                await asyncio.sleep(hook.delay)
                raise TimeoutError("endpoint did not answer")
            hook.sent.append((url, context.check_name, time.monotonic()))
            return True

        monkeypatch.setattr(WebhookNotificationProvider, "send", send)

    def to(self, url: str) -> list[str]:
        return [name for sent_to, name, _ in self.sent if sent_to == url]


async def alert_with(worker_db: Any, providers: list[Any]) -> UUID:
    async with worker_db() as s:
        made = make_alert()
        s.add_all([*providers, made])
        await s.flush()
        s.add_all(
            make_alert_notification_mapping(
                alert_id=made.id, notification_provider_id=p.id
            )
            for p in providers
        )
        await s.commit()
    alert_id: UUID = made.id
    return alert_id


async def queue_for(
    worker_db: Any,
    alert_id: UUID,
    provider_ids: list[UUID],
    name: str,
    check_id: UUID | None = None,
) -> list[UUID]:
    async with worker_db() as s:
        rows = await NotificationCoreService.queue_notifications(
            s,
            provider_ids,
            notification_context(name),
            alert_id=alert_id,
            check_result_id=None,
            check_result_timestamp=None,
            check_id=check_id,
        )
        ids = [row.id for row in rows]
        await s.commit()
    return ids
