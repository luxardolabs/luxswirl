"""What the delivery worker holds a queued notification to when it delivers.

The rate limit counts deliveries by when they were delivered (a backlog
decided long ago is still limited) and never counts a row cut off by a
stopped worker against itself; dead providers, however many, never hold a
healthy one's deliveries behind their timeouts, and a provider's share holds
across server processes; a row whose payload cannot be read
fails alone, and a notification whose provider or check is no longer on its
alert (or whose pair is snoozed, or whose check is deleted) is suppressed.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from app.background import notification_delivery_worker as worker_module
from app.background.notification_delivery_worker import (
    DELIVERY_SLOTS,
    PROVIDER_SHARE,
    DeliveryPool,
    deliver_due_notifications,
)
from app.core.datetime_utils import utc_now
from app.crud.alert_crud import AlertCRUD
from app.db import worker_session
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.check_model import Check
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.services.core import notification_delivery_core_service as delivery_module
from app.services.core.notification_core_service import NotificationCoreService
from app.services.core.notification_delivery_core_service import (
    CLAIM_LEASE,
    NotificationDeliveryCoreService,
)
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


async def _checks_on(worker_db: Any, alert_id: UUID, n: int) -> list[UUID]:
    """`n` checks on the alert, each its own subject: no check's
    notifications wait on another's."""
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.flush()
        checks = [make_check(agent_id=agent.id, display_name=f"c{i}") for i in range(n)]
        s.add_all(checks)
        await s.flush()
        s.add_all(
            make_alert_check_mapping(alert_id=alert_id, check_id=c.id) for c in checks
        )
        await s.commit()
    return [c.id for c in checks]


async def _healthy_beside_dead(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, dead_count: int
) -> tuple[int, float]:
    """30 checks down, one alert, a healthy provider and `dead_count`
    blackholes (each attempt waits out its timeout): how many of the healthy
    provider's 30 were sent, and how long the last took."""
    good_url = "https://good.example.test/hook"
    dead_urls = [f"https://dead{n}.example.test/hook" for n in range(dead_count)]
    hook = _Hooks(slow=set(dead_urls), delay=2.0)
    hook.install(monkeypatch)
    dead = [make_notification_provider(config={"post_url": u}) for u in dead_urls]
    good = make_notification_provider(config={"post_url": good_url})
    alert_id = await _alert_with(worker_db, [*dead, good])
    providers = [*(p.id for p in dead), good.id]
    for n, check_id in enumerate(await _checks_on(worker_db, alert_id, 30)):
        await _queue(worker_db, alert_id, providers, f"c{n}", check_id)

    started = time.monotonic()
    await deliver_committed()

    good_times = [at for url, _, at in hook.sent if url == good_url]
    return len(good_times), max(good_times) - started


async def test_a_healthy_provider_is_not_held_behind_a_dead_one(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healthy provider's 30 are delivered at once, not one batch per
    dead timeout."""
    count, took = await _healthy_beside_dead(worker_db, monkeypatch, 1)
    assert count == 30
    assert took < 1.5, f"the healthy provider's 30 took {took:.1f}s"


async def test_two_dead_providers_do_not_hold_a_healthy_third(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two blackholes with a backlog each, enough to fill every slot
    between them: slots are claimed in turn across providers, so the
    healthy third's 30 still go out at once."""
    count, took = await _healthy_beside_dead(worker_db, monkeypatch, 2)
    assert count == 30
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


async def test_cut_off_delivery_is_not_rate_limited_by_itself(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider limited to one notification per 10 minutes, nothing sent
    in the window. The worker is stopped mid-attempt (a deploy's restart),
    which hands the row back; claimed again at once, the row is not counted
    against its own limit, and it is sent."""
    url = "https://team.example.test/hook"
    hanging = _Hooks(slow={url}, delay=3600)
    hanging.install(monkeypatch)
    provider = make_notification_provider(
        config={"post_url": url}, rate_limit_count=1, rate_limit_window_minutes=10
    )
    alert_id = await _alert_with(worker_db, [provider])
    ids = await _queue(worker_db, alert_id, [provider.id], "svc")
    worker = asyncio.create_task(deliver_due_notifications())
    await asyncio.wait_for(hanging.entered.wait(), 10)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    assert [status for status, _ in await _rows(worker_db, ids)] == ["retrying"]

    answering = _Hooks()
    answering.install(monkeypatch)
    await deliver_due_notifications()

    assert await _rows(worker_db, ids) == [("sent", None)]
    assert answering.to(url) == ["svc"]


async def test_a_row_the_stop_could_not_hand_back_is_claimed_once_its_lease_lapses(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that dies mid-attempt hands nothing back: its row stays
    `sending` until the lease lapses, then is claimed again and sent."""
    url = "https://team.example.test/hook"
    hanging = _Hooks(slow={url}, delay=3600)
    hanging.install(monkeypatch)

    async def unreachable(*_: object) -> int:
        raise ConnectionError("database gone")

    monkeypatch.setattr(NotificationDeliveryCoreService, "release", unreachable)
    provider = make_notification_provider(config={"post_url": url})
    alert_id = await _alert_with(worker_db, [provider])
    ids = await _queue(worker_db, alert_id, [provider.id], "svc")
    worker = asyncio.create_task(deliver_due_notifications())
    await asyncio.wait_for(hanging.entered.wait(), 10)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    assert [status for status, _ in await _rows(worker_db, ids)] == ["sending"]

    answering = _Hooks()
    answering.install(monkeypatch)
    assert await deliver_due_notifications() == 0
    await deliver_due_notifications(now=utc_now() + CLAIM_LEASE + timedelta(seconds=1))

    assert await _rows(worker_db, ids) == [("sent", None)]


async def _dead_backlog(worker_db: Any, dead_count: int) -> UUID:
    """`dead_count` blackholes (each attempt hangs) on one alert, 30 checks
    queued for each: more than the pool's slots."""
    dead = [
        make_notification_provider(config={"post_url": f"https://dead{n}.test/h"})
        for n in range(dead_count)
    ]
    alert_id = await _alert_with(worker_db, dead)
    for n, check_id in enumerate(await _checks_on(worker_db, alert_id, 30)):
        await _queue(worker_db, alert_id, [p.id for p in dead], f"c{n}", check_id)
    return alert_id


async def test_a_provider_with_nothing_in_flight_has_a_slot_dead_ones_cannot_take(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two blackholes take every slot while nothing else is due. A
    notification for a third provider then comes due: the pool claims it at
    once, one attempt past its slots, not after a dead attempt times out."""
    hook = _Hooks(slow={"https://dead0.test/h", "https://dead1.test/h"}, delay=3600)
    hook.install(monkeypatch)
    alert_id = await _dead_backlog(worker_db, 2)
    pool = DeliveryPool(worker_session)
    try:
        dead_taken = await pool.fill()
        good_url = "https://good.example.test/hook"
        good = make_notification_provider(config={"post_url": good_url})
        async with worker_db() as s:
            s.add(good)
            await s.flush()
            s.add(
                make_alert_notification_mapping(
                    alert_id=alert_id, notification_provider_id=good.id
                )
            )
            await s.commit()
        [check_id] = await _checks_on(worker_db, alert_id, 1)
        await _queue(worker_db, alert_id, [good.id], "late", check_id)

        good_taken = await pool.fill()
        await pool.settle(timeout=5)
    finally:
        await pool.cancel()

    assert dead_taken == DELIVERY_SLOTS
    assert good_taken == 1
    assert hook.to(good_url) == ["late"]


async def test_a_providers_share_holds_across_server_processes(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two server processes (two pools) and one blackhole with a backlog:
    the provider's share counts what every process has in flight, so the
    second process claims none of it."""
    hook = _Hooks(slow={"https://dead0.test/h"}, delay=3600)
    hook.install(monkeypatch)
    await _dead_backlog(worker_db, 1)
    first = DeliveryPool(worker_session)
    second = DeliveryPool(worker_session)
    try:
        taken = (await first.fill(), await second.fill())
    finally:
        await first.cancel()
        await second.cancel()

    assert taken == (PROVIDER_SHARE, 0)


async def _late(worker_db: Any, alert_id: UUID, url: str, name: str) -> None:
    """A provider added to the alert, with one notification due now."""
    provider = make_notification_provider(config={"post_url": url})
    async with worker_db() as s:
        s.add(provider)
        await s.flush()
        s.add(
            make_alert_notification_mapping(
                alert_id=alert_id, notification_provider_id=provider.id
            )
        )
        await s.commit()
    [check_id] = await _checks_on(worker_db, alert_id, 1)
    await _queue(worker_db, alert_id, [provider.id], name, check_id)


async def test_no_number_of_dead_providers_holds_a_healthy_one_behind_them(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two blackholes fill every slot; a third, and then a fourth, comes due
    with nothing in flight and each starts its first attempt; a healthy
    provider coming due after them is still claimed at once."""
    dead = [f"https://dead{n}.test/h" for n in range(4)]
    hook = _Hooks(slow=set(dead), delay=3600)
    hook.install(monkeypatch)
    alert_id = await _dead_backlog(worker_db, 2)
    pool = DeliveryPool(worker_session)
    try:
        first = await pool.fill()
        await _late(worker_db, alert_id, dead[2], "third-dead")
        await _late(worker_db, alert_id, dead[3], "fourth-dead")
        late_dead = await pool.fill()
        await _late(worker_db, alert_id, "https://good.test/h", "late")
        good_taken = await pool.fill()
        await pool.settle(timeout=5)
    finally:
        await pool.cancel()

    assert (first, late_dead, good_taken) == (DELIVERY_SLOTS, 2, 1)
    assert hook.to("https://good.test/h") == ["late"]


async def test_a_graceful_stop_leaves_nothing_counted_in_flight(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider's share is in flight when the worker stops: the stop hands
    the rows back, so the restarted process claims the provider's rows at
    once, not after the lease lapses."""
    hanging = _Hooks(slow={"https://dead0.test/h"}, delay=3600)
    hanging.install(monkeypatch)
    await _dead_backlog(worker_db, 1)
    before = DeliveryPool(worker_session)
    assert await before.fill() == PROVIDER_SHARE
    await before.cancel()

    answering = _Hooks()
    answering.install(monkeypatch)
    restarted = DeliveryPool(worker_session)
    try:
        taken = await restarted.fill()
        await restarted.settle(timeout=5)
    finally:
        await restarted.cancel()

    assert taken == PROVIDER_SHARE


async def test_a_host_clock_running_fast_does_not_claim_a_live_lease(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One server process holds a provider's row under its lease; another,
    on a host whose clock runs past the lease, claims nothing: every
    process judges leases by the database's clock."""
    hanging = _Hooks(slow={"https://dead0.test/h"}, delay=3600)
    hanging.install(monkeypatch)
    await _dead_backlog(worker_db, 1)
    holder = DeliveryPool(worker_session)
    fast = utc_now() + CLAIM_LEASE + timedelta(minutes=1)
    try:
        assert await holder.fill() == PROVIDER_SHARE
        monkeypatch.setattr(worker_module, "utc_now", lambda: fast, raising=False)
        monkeypatch.setattr(delivery_module, "utc_now", lambda: fast, raising=False)
        other = DeliveryPool(worker_session)
        try:
            taken = await other.fill()
        finally:
            await other.cancel()
    finally:
        await holder.cancel()

    assert taken == 0
