"""A host clock out of step with the database never re-notifies an episode, and
a pool never claims a row it still holds.

An episode is judged against `sent_at`, on the host's clock like every value it
is compared with. A pool whose record of an ended attempt keeps failing past
the lease never claims that row again.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from alerts.test_silence_alerts import _agent, _alert, _check, _evaluate, sent
from app.background import notification_delivery_worker as worker_module
from app.background.notification_delivery_worker import DeliveryPool
from app.core import datetime_utils
from app.db import worker_session
from app.services.core import alert_evaluation_core_service as evaluation_module
from app.services.core import notification_core_service as notification_module
from app.services.core import silence_alert_core_service as silence_module
from app.services.core.notification_delivery_core_service import (
    CLAIM_LEASE,
    NotificationDeliveryCoreService,
)
from fixtures.factories import make_notification_provider
from notifications.test_outbox_delivery_rules import _alert_with, _Hooks, _queue
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

__all__ = ["sent"]

_real_utc_now = datetime_utils.utc_now


async def _offline_passes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    """An agent last heard from 90 s ago (offline past 5 s x 3 + 60 s), judged
    by a server whose host clock is `skew` ahead of the database's; three
    silence passes."""

    def host_now() -> Any:
        return _real_utc_now() + skew

    monkeypatch.setattr(silence_module, "utc_now", host_now)
    monkeypatch.setattr(evaluation_module, "utc_now", host_now)
    monkeypatch.setattr(notification_module, "utc_now", host_now)
    monkeypatch.setattr(worker_module, "utc_now", host_now)
    # The heartbeat was received, and stamped, by this server's host clock.
    agent = await _agent(
        db, heartbeat_interval=5, last_seen=host_now() - timedelta(seconds=90)
    )
    check = await _check(db, agent, last_result_ago=timedelta(minutes=10))
    await _alert(db, "agent_offline", [check])
    for _ in range(3):
        await _evaluate(db)


async def test_a_host_clock_ahead_notifies_an_offline_agent_once(
    db: AsyncSession, sent: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host clock two minutes ahead of the database: one offline episode
    notifies once."""
    await _offline_passes(db, monkeypatch, timedelta(minutes=2))
    assert len(sent) == 1, [c.status_label for c in sent]


async def test_control_host_clock_in_step_notifies_an_offline_agent_once(
    db: AsyncSession, sent: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same with the host clock in step notifies once."""
    await _offline_passes(db, monkeypatch, timedelta(0))
    assert len(sent) == 1, [c.status_label for c in sent]


async def _record_fails_then_database_returns(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, outage: timedelta
) -> list[str]:
    """One notification; its attempt is delivered, then its record fails (the
    database unreachable) and keeps failing for `outage`. The worker loop's
    next pass then runs as it does: fill first, then settle."""
    hook = _Hooks()
    hook.install(monkeypatch)
    url = "https://p.test/h"
    provider = make_notification_provider(config={"post_url": url})
    alert_id = await _alert_with(worker_db, [provider])
    await _queue(worker_db, alert_id, [provider.id], "down")
    start = datetime_utils.utc_now() + timedelta(seconds=1)

    original = NotificationDeliveryCoreService.record
    failures = {"left": 1}

    async def record(db: Any, claimed: Any, outcome: Any, now: Any) -> bool:
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionRefusedError("database unreachable")
        return await original(db, claimed, outcome, now)

    monkeypatch.setattr(NotificationDeliveryCoreService, "record", staticmethod(record))

    pool = DeliveryPool(worker_session, now=start)
    await pool.fill()
    with pytest.raises(ConnectionRefusedError):
        await pool.settle(timeout=None)
    # The database answers again, `outage` later (the clock the pool reads).
    pool._now = start + outage
    await pool.fill()
    while pool.busy:
        await pool.settle(timeout=None)
    return hook.to(url)


async def test_a_record_failing_past_the_lease_sends_the_notification_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool holds the delivered outcome; the database comes back after the
    lease lapsed: the notification must still go out once."""
    sends = await _record_fails_then_database_returns(
        worker_db, monkeypatch, CLAIM_LEASE + timedelta(seconds=5)
    )
    assert sends == ["down"], sends


async def test_control_a_record_failing_briefly_sends_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the database back within the lease: sent once, then recorded."""
    sends = await _record_fails_then_database_returns(
        worker_db, monkeypatch, timedelta(seconds=5)
    )
    assert sends == ["down"], sends
