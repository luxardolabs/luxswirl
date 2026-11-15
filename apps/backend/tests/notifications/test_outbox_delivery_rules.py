"""What the delivery worker holds a queued notification to when it delivers.

The rate limit counts deliveries by when they were delivered (a backlog
decided long ago is still limited), one dead provider never holds a healthy
one's deliveries behind its timeouts, a row whose payload cannot be read
fails alone, and a notification whose provider or check is no longer on its
alert (or whose pair is snoozed, or whose check is deleted) is suppressed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.crud.alert_crud import AlertCRUD
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.check_model import Check
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_notification_provider,
)
from fixtures.outbox import deliver_committed
from sqlalchemy import delete, select, update

pytestmark = pytest.mark.integration


def _context(name: str) -> NotificationContext:
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


class _Hooks:
    """Records each send per provider url, with when it was made; a url in
    `slow` waits `delay` seconds and then fails, as a blackholed endpoint
    does."""

    def __init__(self, slow: set[str] | None = None, delay: float = 0.0) -> None:
        self.slow = slow or set()
        self.delay = delay
        self.sent: list[tuple[str, str, float]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook = self

        async def send(self: Any, context: NotificationContext) -> bool:
            url = self.config["post_url"]
            if url in hook.slow:
                await asyncio.sleep(hook.delay)
                raise TimeoutError("endpoint did not answer")
            hook.sent.append((url, context.check_name, time.monotonic()))
            return True

        monkeypatch.setattr(WebhookNotificationProvider, "send", send)

    def to(self, url: str) -> list[str]:
        return [name for sent_to, name, _ in self.sent if sent_to == url]


async def _alert_with(worker_db: Any, providers: list[Any]) -> UUID:
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


async def _queue(
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
            _context(name),
            alert_id=alert_id,
            check_result_id=None,
            check_result_timestamp=None,
            check_id=check_id,
        )
        ids = [row.id for row in rows]
        await s.commit()
    return ids


async def _rows(worker_db: Any, ids: list[UUID]) -> list[tuple[str, str | None]]:
    async with worker_db() as s:
        result = await s.execute(
            select(
                NotificationLog.id,
                NotificationLog.status,
                NotificationLog.error_message,
            ).where(NotificationLog.id.in_(ids))
        )
        by_id = {log_id: (str(status), error) for log_id, status, error in result}
    return [by_id[log_id] for log_id in ids]


async def test_rate_limit_holds_for_a_backlog_decided_before_the_window(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider limited to one notification per 10 minutes, five waiting
    since they were decided 20 minutes ago (the worker was down): delivered
    now, one goes out and the rest are rate limited."""
    hook = _Hooks()
    hook.install(monkeypatch)
    provider = make_notification_provider(
        rate_limit_count=1, rate_limit_window_minutes=10
    )
    alert_id = await _alert_with(worker_db, [provider])
    ids: list[UUID] = []
    for n in range(5):
        ids += await _queue(worker_db, alert_id, [provider.id], f"c{n}")
    decided = utc_now() - timedelta(minutes=20)
    async with worker_db() as s:
        await s.execute(
            update(NotificationLog)
            .where(NotificationLog.id.in_(ids))
            .values(sent_at=decided, next_attempt_at=decided)
        )
        await s.commit()

    await deliver_committed()

    statuses = [status for status, _ in await _rows(worker_db, ids)]
    assert statuses.count("sent") == 1, statuses
    assert statuses.count("rate_limited") == 4, statuses


async def test_a_healthy_provider_is_not_held_behind_a_dead_one(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30 checks down, one alert, two providers, one of them a blackhole
    (each attempt waits out its timeout). The healthy provider's 30 are
    delivered at once, not one batch per dead timeout."""
    dead_url = "https://dead.example.test/hook"
    good_url = "https://good.example.test/hook"
    hook = _Hooks(slow={dead_url}, delay=2.0)
    hook.install(monkeypatch)
    dead = make_notification_provider(config={"post_url": dead_url})
    good = make_notification_provider(config={"post_url": good_url})
    alert_id = await _alert_with(worker_db, [dead, good])
    for n in range(30):
        await _queue(worker_db, alert_id, [dead.id, good.id], f"c{n}")

    started = time.monotonic()
    await deliver_committed()

    good_times = [at for url, _, at in hook.sent if url == good_url]
    assert len(good_times) == 30
    took = max(good_times) - started
    assert took < 1.5, f"the healthy provider's 30 took {took:.1f}s"


async def test_one_unreadable_payload_does_not_stop_every_other_delivery(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued row whose payload this release cannot read (one written by
    another release, a field missing) fails on its own; every other row is
    still delivered."""
    url = "https://other.example.test/hook"
    hook = _Hooks()
    hook.install(monkeypatch)
    provider = make_notification_provider(config={"post_url": url})
    alert_id = await _alert_with(worker_db, [provider])
    unreadable = await _queue(worker_db, alert_id, [provider.id], "old")
    async with worker_db() as s:
        row = await s.get(NotificationLog, unreadable[0])
        assert row is not None and row.payload is not None
        legacy = dict(row.payload)
        legacy.pop("check_name")
        row.payload = legacy
        await s.commit()
    other = make_notification_provider(config={"post_url": url})
    other_alert = await _alert_with(worker_db, [other])
    for n in range(3):
        await _queue(worker_db, other_alert, [other.id], f"unrelated{n}")

    await deliver_committed()

    assert sorted(hook.to(url)) == ["unrelated0", "unrelated1", "unrelated2"]
    assert await _rows(worker_db, unreadable) == [("failed", "Unreadable payload")]


async def _edited_before_delivery(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, edit: str
) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Queue one notification about a mapped check, apply `edit`, deliver."""
    url = "https://team.example.test/hook"
    hook = _Hooks()
    hook.install(monkeypatch)
    provider = make_notification_provider(config={"post_url": url})
    alert_id = await _alert_with(worker_db, [provider])
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        check = make_check(agent_id=agent.id, display_name="svc")
        s.add(check)
        await s.flush()
        s.add(make_alert_check_mapping(alert_id=alert_id, check_id=check.id))
        await s.commit()
    ids = await _queue(worker_db, alert_id, [provider.id], "svc", check.id)
    async with worker_db() as s:
        if edit == "unmap_provider":
            await AlertCRUD.replace_provider_mappings(s, alert_id, [])
        elif edit == "disable_provider_mapping":
            await s.execute(
                update(AlertNotificationMapping)
                .where(AlertNotificationMapping.alert_id == alert_id)
                .values(is_enabled=False)
            )
        elif edit == "unmap_check":
            await AlertCRUD.delete_check_mapping(s, alert_id, check.id)
        elif edit == "snooze":
            now = utc_now()
            await AlertCRUD.snooze_check_mapping(
                s,
                alert_id,
                check.id,
                now=now,
                extend_by=timedelta(hours=1),
                latest=now + timedelta(days=1),
            )
        elif edit == "delete_check":
            await s.execute(delete(Check).where(Check.id == check.id))
        await s.commit()
    await deliver_committed()
    return hook.to(url), await _rows(worker_db, ids)


async def test_a_provider_taken_off_the_alert_before_delivery_is_not_sent_to(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, rows = await _edited_before_delivery(worker_db, monkeypatch, "unmap_provider")
    assert sent == []
    assert rows == [("suppressed", "Notification provider is not on the alert")]


async def test_a_provider_disabled_on_the_alert_before_delivery_is_not_sent_to(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, rows = await _edited_before_delivery(
        worker_db, monkeypatch, "disable_provider_mapping"
    )
    assert sent == []
    assert rows == [("suppressed", "Notification provider is not on the alert")]


async def test_a_check_taken_off_the_alert_before_delivery_is_not_notified(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, rows = await _edited_before_delivery(worker_db, monkeypatch, "unmap_check")
    assert sent == []
    assert rows == [("suppressed", "Check is not on the alert")]


async def test_a_check_snoozed_before_delivery_is_not_notified(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, rows = await _edited_before_delivery(worker_db, monkeypatch, "snooze")
    assert sent == []
    assert rows == [("suppressed", "Alert is snoozed for the check")]


async def test_a_check_deleted_before_delivery_is_not_notified(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, rows = await _edited_before_delivery(worker_db, monkeypatch, "delete_check")
    assert sent == []
    assert rows == [("suppressed", "Check is deleted")]


async def test_a_check_still_on_the_alert_is_notified(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, rows = await _edited_before_delivery(worker_db, monkeypatch, "none")
    assert sent == ["svc"]
    assert rows == [("sent", None)]
