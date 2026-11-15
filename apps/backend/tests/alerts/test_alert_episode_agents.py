"""An episode's end across timeouts and across a check's agents.

A threshold recovery after a timeout still tells of the breach before it. A
check several agents run ends an outage only when each agent's latest result
is clear: one agent up while another is down is one outage, announced once.
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


async def _setup(db: AsyncSession, agents: int = 1, **alert: Any):
    made_agents = [make_agent() for _ in range(agents)]
    db.add_all(made_agents)
    await db.flush()
    check = make_check(agent_id=made_agents[0].id)
    made = make_alert(**{"resend_interval_minutes": None, **alert})
    provider = make_notification_provider()
    db.add_all([check, made, provider])
    await db.flush()
    notif = make_alert_notification_mapping(
        alert_id=made.id, notification_provider_id=provider.id
    )
    db.add_all([make_alert_check_mapping(alert_id=made.id, check_id=check.id), notif])
    await db.flush()
    return made_agents, check, made, provider, notif


async def _feed(
    db: AsyncSession,
    agents: list[Any],
    check: Any,
    runs: list[tuple[int, bool, float | None]],
    start: Any,
) -> None:
    """Each run is (agent index, success, latency), 30 s apart."""
    for n, (who, success, latency) in enumerate(runs):
        result = make_check_result(
            check_id=check.id,
            agent_id=agents[who].id,
            success=success,
            latency_ms=latency,
            timestamp=start + timedelta(seconds=30 * n),
        )
        db.add(result)
        await db.flush()
        await AlertEvaluationCoreService.evaluate_check_result(db, result)
        await deliver_in(db)


def _told(sent: list[NotificationContext]) -> list[str]:
    return [f"{'recovery' if c.is_recovery else 'alert'}:{c.latency_ms}" for c in sent]


# --- A: a timeout between the breach and the safe result eats the recovery --


async def test_threshold_recovery_after_a_timeout_is_announced(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """Slow (announced), then a timeout (no latency), then fast again. The gate
    asks only whether the result just before breached, a timeout did not, so no
    recovery; and _note_cleared then writes the cleared row, so it never will."""
    agents, check, _, _, _ = await _setup(db, **THRESHOLD)
    runs = [(0, True, 500.0), (0, False, None), (0, True, 50.0), (0, True, 50.0)]
    await _feed(db, agents, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:500.0", "recovery:50.0"]


async def test_control_threshold_recovery_without_a_timeout(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    agents, check, _, _, _ = await _setup(db, **THRESHOLD)
    runs = [(0, True, 500.0), (0, True, 50.0), (0, True, 50.0)]
    await _feed(db, agents, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:500.0", "recovery:50.0"]


# --- B: a replicate check with one agent down re-announces every interval ----


async def test_replicate_check_one_agent_down_is_announced_once(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """Replicate check on two agents; agent 0 cannot reach the target, agent 1
    can. notify_on_recovery off. Every success from agent 1 writes a cleared
    row, so every failure from agent 0 is announced as a new outage: one DOWN
    per interval, never an UP between them."""
    agents, check, _, _, _ = await _setup(db, agents=2, notify_on_recovery=False)
    runs = [(0, False, None), (1, True, 20.0)] * 4
    await _feed(db, agents, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:None"]


async def test_control_single_agent_steady_outage_is_announced_once(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    agents, check, _, _, _ = await _setup(db, notify_on_recovery=False)
    runs = [(0, False, None)] * 4
    await _feed(db, agents, check, runs, utc_now() - timedelta(minutes=10))

    assert _told(sent) == ["alert:None"]


# --- C: a clear while the alert has no provider is never recorded -----------
