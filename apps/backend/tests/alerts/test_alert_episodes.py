"""A repeated_failure alert announces its outage and its end.

An alert's notifications are de-duplicated by its state key: a fire whose key
matches the last notification's is a repeat of it. A repeated_failure alert's
outage and recovery have keys of their own, as a status_change alert's do.
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


async def _run(
    db: AsyncSession,
    sent: list[NotificationContext],
    runs: list[tuple[bool, float]],
    **alert: Any,
) -> list[bool]:
    """An alert on a check, with a webhook, and the check's runs (success,
    latency) a minute apart, each decided and delivered in turn. Whether each
    notification sent told of a recovery, in order."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    made = make_alert(resend_interval_minutes=None, **alert)
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
    start = utc_now() - timedelta(minutes=len(runs))
    for n, (success, latency) in enumerate(runs):
        result = make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=success,
            latency_ms=latency,
            timestamp=start + timedelta(minutes=n),
        )
        db.add(result)
        await db.flush()
        await AlertEvaluationCoreService.evaluate_check_result(db, result)
        await deliver_in(db)
    return [context.is_recovery for context in sent]


DOWN = (False, 10.0)
UP = (True, 10.0)
REPEATED = {
    "trigger_type": "repeated_failure",
    "trigger_config": {"on_status": ["error"], "consecutive_failures": 2},
}


async def test_a_repeated_failure_alert_announces_its_recovery(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    told = await _run(db, sent, [DOWN, DOWN, UP], **REPEATED)

    assert told == [False, True]


async def test_a_repeated_failure_alert_announces_a_second_outage(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    told = await _run(db, sent, [DOWN, DOWN, UP, DOWN, DOWN], **REPEATED)

    assert told == [False, True, False]


async def test_control_a_steady_outage_is_announced_once(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    told = await _run(db, sent, [DOWN, DOWN, DOWN, DOWN], **REPEATED)

    assert told == [False]
