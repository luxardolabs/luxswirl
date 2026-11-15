"""The edges of an alert's episodes: timeouts, old breaches, parent outages.

A result with no latency (a timeout) neither ends a threshold breach nor starts
a second one. A recovery tells of the result before it: a breach long over gets
none, and one hidden behind a parent's outage still gets one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from app.core.datetime_utils import utc_now
from app.notifications.providers.base import NotificationContext
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_check_result,
    make_notification_provider,
)
from fixtures.outbox import deliver_in
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

THRESHOLD = {
    "trigger_type": "threshold",
    "trigger_config": {"metric": "latency_ms", "operator": ">", "value": 100},
}


@pytest.fixture
def sent() -> Iterator[list[NotificationContext]]:
    contexts: list[NotificationContext] = []

    async def _send(self: Any, context: NotificationContext) -> bool:
        contexts.append(context)
        return True

    with patch(
        "app.notifications.providers.webhook.WebhookNotificationProvider.send", _send
    ):
        yield contexts


async def _setup(db: AsyncSession, *, with_parent: bool = False, **alert: Any):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    parent = make_check(agent_id=agent.id) if with_parent else None
    if parent is not None:
        db.add(parent)
        await db.flush()
    check = make_check(
        agent_id=agent.id, depends_on_check_id=parent.id if parent else None
    )
    made = make_alert(**{"resend_interval_minutes": None, **alert})
    provider = make_notification_provider()
    db.add_all([check, made, provider])
    await db.flush()
    db.add_all(
        [
            make_alert_check_mapping(alert_id=made.id, check_id=check.id),
            make_alert_notification_mapping(
                alert_id=made.id, notification_provider_id=provider.id
            ),
        ]
    )
    await db.flush()
    return agent, parent, check, made


async def _feed(
    db: AsyncSession,
    agent: Any,
    parent: Any,
    check: Any,
    runs: list[tuple[bool, float | None, bool | None]],
    start: Any,
) -> None:
    """Each run is (success, latency, parent_success or None), a minute apart."""
    for n, (success, latency, parent_ok) in enumerate(runs):
        at = start + timedelta(minutes=n)
        if parent_ok is not None:
            db.add(
                make_check_result(
                    check_id=parent.id,
                    agent_id=agent.id,
                    success=parent_ok,
                    latency_ms=5.0 if parent_ok else None,
                    timestamp=at - timedelta(seconds=1),
                )
            )
        result = make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=success,
            latency_ms=latency,
            timestamp=at,
        )
        db.add(result)
        await db.flush()
        await AlertEvaluationCoreService.evaluate_check_result(db, result)
        await deliver_in(db)


def _told(sent: list[NotificationContext]) -> list[str]:
    return [f"{'recovery' if c.is_recovery else 'alert'}:{c.latency_ms}" for c in sent]


# --- Finding 1: a timeout (no latency) mid-breach re-announces the breach --


async def test_a_timeout_mid_breach_is_not_a_second_breach(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """Slow service that intermittently times out: 500 ms, timeout, 500 ms,
    timeout, 500 ms. A timeout is not a recovery (none is sent), so each later
    breach repeats the first one."""
    agent, parent, check, _ = await _setup(db, **THRESHOLD)
    runs = [
        (True, 500.0, None),
        (False, None, None),
        (True, 500.0, None),
        (False, None, None),
        (True, 500.0, None),
    ]
    await _feed(db, agent, parent, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:500.0"]


async def test_control_a_steady_breach_is_announced_once(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    agent, parent, check, _ = await _setup(db, **THRESHOLD)
    runs = [(True, 500.0, None)] * 3
    await _feed(db, agent, parent, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:500.0"]


# --- Finding 2: a recovery for a breach that ended long ago ----------------


async def test_threshold_recovery_is_not_sent_for_a_breach_long_over(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """The breach row was written without a recovery after it (by the code
    before this change, or with notify_on_recovery off); the check has been on
    the safe side ever since. Recoveries on (the upgrade, or an edit): the next
    safe result announces a recovery from a breach that ended hours ago."""
    agent, parent, check, alert = await _setup(
        db, notify_on_recovery=False, **THRESHOLD
    )
    start = utc_now() - timedelta(hours=5)
    await _feed(
        db,
        agent,
        parent,
        check,
        [(True, 500.0, None)] + [(True, 50.0, None)] * 3,
        start,
    )
    assert _told(sent) == ["alert:500.0"]
    alert.notify_on_recovery = True
    await db.flush()
    await _feed(db, agent, parent, check, [(True, 50.0, None)], utc_now())

    assert _told(sent) == ["alert:500.0"]


async def test_control_status_recovery_is_not_sent_for_an_outage_long_over(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    agent, parent, check, alert = await _setup(db, notify_on_recovery=False)
    start = utc_now() - timedelta(hours=5)
    await _feed(
        db,
        agent,
        parent,
        check,
        [(False, None, None)] + [(True, 50.0, None)] * 3,
        start,
    )
    alert.notify_on_recovery = True
    await db.flush()
    await _feed(db, agent, parent, check, [(True, 50.0, None)], utc_now())

    assert _told(sent) == ["alert:None"]


# --- Finding 3: a threshold recovery is lost after a parent_down row -------


async def test_threshold_recovery_survives_a_parent_outage(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """Breach announced; the parent goes down while the leaf still breaches
    (suppressed row `parent_down`); the parent comes back and the leaf is on
    the safe side: the breach's end is never told."""
    agent, parent, check, _ = await _setup(db, with_parent=True, **THRESHOLD)
    runs = [
        (True, 500.0, True),
        (True, 500.0, False),
        (True, 50.0, True),
    ]
    await _feed(db, agent, parent, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:500.0", "recovery:50.0"]


async def test_control_status_recovery_survives_a_parent_outage(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    agent, parent, check, _ = await _setup(db, with_parent=True)
    runs = [
        (False, None, True),
        (False, None, False),
        (True, 50.0, True),
    ]
    await _feed(db, agent, parent, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:None", "recovery:50.0"]


# --- Finding 4: each failing result scans the whole outage -----------------
