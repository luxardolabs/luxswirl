"""A graceful stop never sends a notification twice, and a host's clock never
delays its own.

A stop that lands while the worker records one ended attempt records every
attempt that ended and hands back only those it cut off. Notifications are
queued, and judged due, by the database's clock.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from app.background import notification_delivery_worker as worker_module
from app.core.datetime_utils import utc_now
from app.services.core import notification_core_service as core_module
from app.services.core.notification_delivery_core_service import (
    NotificationDeliveryCoreService,
)
from fixtures.factories import make_notification_provider
from fixtures.outbox import deliver_committed
from notifications.test_outbox_delivery_rules import _alert_with, _Hooks, _queue

pytestmark = pytest.mark.integration


async def _stop_while_recording(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch, hook: _Hooks
) -> list[str]:
    """Two providers, one notification each; the running worker is stopped
    while it records the first ended attempt (its database round trip in
    progress), the second attempt also ended. A restarted worker then
    delivers whatever is due."""
    urls = ["https://one.test/h", "https://two.test/h"]
    providers = [make_notification_provider(config={"post_url": u}) for u in urls]
    alert_id = await _alert_with(worker_db, providers)
    await _queue(worker_db, alert_id, [p.id for p in providers], "down")

    original = NotificationDeliveryCoreService.record
    recording = asyncio.Event()

    async def record(db: Any, claimed: Any, outcome: Any, now: Any) -> bool:
        # The first record's database round trip is in progress when the
        # stop lands; the rest answer as the database does.
        if not recording.is_set():
            recording.set()
            await asyncio.Event().wait()
        return await original(db, claimed, outcome, now)

    monkeypatch.setattr(NotificationDeliveryCoreService, "record", staticmethod(record))
    worker_module.start_notification_delivery_worker()
    await asyncio.wait_for(recording.wait(), 10)
    await asyncio.sleep(0.2)
    await worker_module.stop_notification_delivery_worker()
    monkeypatch.setattr(
        NotificationDeliveryCoreService, "record", staticmethod(original)
    )

    await deliver_committed()
    return urls


async def test_a_stop_while_recording_sends_nothing_twice(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both providers answered before the stop: neither notification may go
    out twice when the worker restarts."""
    hook = _Hooks()
    hook.install(monkeypatch)
    urls = await _stop_while_recording(worker_db, monkeypatch, hook)
    assert [len(hook.to(u)) for u in urls] == [1, 1], hook.sent


async def test_control_stop_hands_back_an_attempt_cut_off_mid_send(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: an attempt truly cut off by the stop is handed back and sent
    once by the restarted worker."""
    hanging = _Hooks(slow={"https://slow.test/h"}, delay=3600)
    hanging.install(monkeypatch)
    provider = make_notification_provider(config={"post_url": "https://slow.test/h"})
    alert_id = await _alert_with(worker_db, [provider])
    await _queue(worker_db, alert_id, [provider.id], "down")
    worker_module.start_notification_delivery_worker()
    await asyncio.wait_for(hanging.entered.wait(), 10)
    await worker_module.stop_notification_delivery_worker()

    answering = _Hooks()
    answering.install(monkeypatch)
    await deliver_committed()
    assert answering.to("https://slow.test/h") == ["down"]


async def test_a_host_clock_ahead_of_the_database_delivers_at_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server whose host clock runs two minutes ahead of the database
    decides an outage: it goes out on this pass."""
    hook = _Hooks()
    hook.install(monkeypatch)
    provider = make_notification_provider(config={"post_url": "https://p.test/h"})
    alert_id = await _alert_with(worker_db, [provider])
    fast = utc_now() + timedelta(minutes=2)
    monkeypatch.setattr(core_module, "utc_now", lambda: fast)
    await _queue(worker_db, alert_id, [provider.id], "down")

    await deliver_committed()
    assert hook.to("https://p.test/h") == ["down"]


async def test_control_host_clock_in_step_delivers_at_once(
    worker_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same with the host clock in step goes out on this pass."""
    hook = _Hooks()
    hook.install(monkeypatch)
    provider = make_notification_provider(config={"post_url": "https://p.test/h"})
    alert_id = await _alert_with(worker_db, [provider])
    await _queue(worker_db, alert_id, [provider.id], "down")

    await deliver_committed()
    assert hook.to("https://p.test/h") == ["down"]
