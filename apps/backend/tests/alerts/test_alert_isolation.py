"""One alert that fails while it is evaluated stops no other alert on the check,
and an alert whose state key would not fit the log column still sends once.

A stored trigger_config of the wrong type, or any other per-alert bug, left the
evaluation loop. A threshold value of many digits (the web form stored it as
given) made a state key longer than `notification_hash`: the log row failed to
write after the notification went out, so dedup never saw it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.core.exceptions import ValidationException
from app.models.enum_model import AlertTriggerType
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.schemas.alert_schema import AlertCreate
from app.services.core.alert_core_service import AlertCoreService
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_an_alert_that_raises_leaves_the_others_to_fire(
    db: AsyncSession, app_logs: list[logging.LogRecord]
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    broken = make_alert(name="broken")
    good = make_alert(name="good")
    db.add_all([check, broken, good])
    await db.flush()
    db.add_all(
        [
            make_alert_check_mapping(alert_id=broken.id, check_id=check.id),
            make_alert_check_mapping(alert_id=good.id, check_id=check.id),
        ]
    )
    result = make_check_result(check_id=check.id, agent_id=agent.id, success=False)
    db.add(result)
    await db.flush()
    fired: list[UUID] = []

    async def should_fire(_db: Any, alert: Any, _check: Any, _result: Any) -> bool:
        if alert.id == broken.id:
            raise TypeError("'>' not supported between instances of 'str' and 'int'")
        return True

    async def fire(_db: Any, alert: Any, _check: Any, _result: Any) -> None:
        fired.append(alert.id)

    with (
        patch.object(AlertEvaluationCoreService, "_should_fire_alert", should_fire),
        patch.object(AlertEvaluationCoreService, "_fire_alert", fire),
    ):
        await AlertEvaluationCoreService.evaluate_check_result(db, result)

    assert fired == [good.id]
    failures = [
        r
        for r in app_logs
        if r.levelno >= logging.ERROR and str(broken.id) in str(r.__dict__)
    ]
    assert failures, [r.getMessage() for r in app_logs]


# A threshold value of 51 digits: its state key is 74 characters.
HUGE = 10**50


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


async def _two_failures(db: AsyncSession, value: int) -> tuple[Any, list[Any]]:
    """A threshold alert on a check, with a webhook, and two failed results."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    threshold = make_alert(
        name="threshold",
        trigger_type="threshold",
        trigger_config={"metric": "latency_ms", "operator": "<", "value": value},
        resend_interval_minutes=None,
    )
    provider = make_notification_provider()
    db.add_all([check, threshold, provider])
    await db.flush()
    db.add_all(
        [
            make_alert_check_mapping(alert_id=threshold.id, check_id=check.id),
            make_alert_notification_mapping(
                alert_id=threshold.id, notification_provider_id=provider.id
            ),
        ]
    )
    now = utc_now()
    results = [
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=False,
            timestamp=now - timedelta(minutes=1),
        ),
        make_check_result(
            check_id=check.id, agent_id=agent.id, success=False, timestamp=now
        ),
    ]
    db.add_all(results)
    await db.flush()
    return threshold, results


async def _logged(db: AsyncSession, alert_id: UUID) -> list[NotificationLog]:
    rows = await db.execute(
        select(NotificationLog).where(NotificationLog.alert_id == alert_id)
    )
    return list(rows.scalars().all())


@pytest.mark.parametrize("value", [1_000_000, HUGE])
async def test_a_threshold_alert_sends_once_and_logs_it(
    db: AsyncSession, sent: list[NotificationContext], value: int
) -> None:
    """However long its state key, the alert's send is logged, so the second
    result in the same state is deduplicated."""
    threshold, results = await _two_failures(db, value)
    for result in results:
        await AlertEvaluationCoreService.evaluate_check_result(db, result)

    assert len([c for c in sent if c.alert_name == "threshold"]) == 1
    assert len(await _logged(db, threshold.id)) == 1


async def test_the_web_path_stores_a_threshold_config_in_its_validated_form(
    db: AsyncSession,
) -> None:
    alert = await AlertCoreService.create_alert(
        db,
        AlertCreate(
            name="from-the-form",
            trigger_type=AlertTriggerType.THRESHOLD,
            trigger_config={"metric": "latency_ms", "operator": "<", "value": HUGE},
            notification_provider_ids=[],
        ),
    )
    assert alert.trigger_config["value"] == float(HUGE)


async def test_a_trigger_config_of_the_wrong_type_is_refused_by_every_path(
    db: AsyncSession,
) -> None:
    with pytest.raises(ValidationException, match="consecutive_failures"):
        await AlertCoreService.create_alert(
            db,
            AlertCreate(
                name="bad",
                trigger_type=AlertTriggerType.STATUS_CHANGE,
                trigger_config={"on_status": ["error"], "consecutive_failures": "3"},
                notification_provider_ids=[],
            ),
        )
