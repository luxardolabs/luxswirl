"""What judging a result of a check several agents run reads, and whom a breach
names.

The agents that run the check are read with their own columns alone, never
every check of every active agent; a latency breach told with another agent's
result names that agent and its latency.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from app.core.datetime_utils import utc_now
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
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
from sqlalchemy import event, select
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


async def _alerted_check(
    db: AsyncSession, owners: list[Any], mode: str, **alert: Any
) -> Check:
    check = make_check(
        agent_id=owners[0].id,
        assignment_mode=mode,
        agent_selector=(
            {"agent_ids": [a.agent_name for a in owners]} if mode != "manual" else None
        ),
    )
    alert_row = make_alert(**{"resend_interval_minutes": None, **alert})
    provider = make_notification_provider()
    db.add_all([check, alert_row, provider])
    await db.flush()
    db.add_all(
        [
            make_alert_check_mapping(alert_id=alert_row.id, check_id=check.id),
            make_alert_notification_mapping(
                alert_id=alert_row.id, notification_provider_id=provider.id
            ),
        ]
    )
    await db.flush()
    return check


async def _reload(db: AsyncSession, result_id: Any) -> CheckResult:
    """The result as the decision worker reads it, in a session holding
    nothing else."""
    db.expunge_all()
    return (
        await db.execute(select(CheckResult).where(CheckResult.id == result_id))
    ).scalar_one()


# --- 1. judging one result loads every check of every active agent ----------


@pytest.mark.parametrize("mode", ["manual", "replicate", "distribute"], ids=lambda m: m)
async def test_judging_a_result_does_not_load_the_fleets_checks(
    db: AsyncSession, sent: list[NotificationContext], mode: str
) -> None:
    """Two agents run a check with a status alert. Ten other active agents,
    unrelated to it, run 20 checks each. Judging one result of the check
    must not load those 200 checks: on a real fleet (hundreds of agents,
    thousands of checks) that is every check row per result decided.
    Control: the same check in manual mode."""
    owners = [make_agent(), make_agent()]
    bystanders = [make_agent() for _ in range(10)]
    db.add_all([*owners, *bystanders])
    await db.flush()
    db.add_all([make_check(agent_id=b.id) for b in bystanders for _ in range(20)])
    await db.flush()
    check = await _alerted_check(db, owners, mode, trigger_type="status_change")
    at = utc_now() - timedelta(minutes=10)
    result = make_check_result(
        check_id=check.id, agent_id=owners[0].id, success=True, timestamp=at
    )
    db.add(result)
    await db.flush()

    bystander_ids = {b.id for b in bystanders}
    judged = await _reload(db, result.id)
    loaded: list[Any] = []

    def _on_load(target: Check, context: Any) -> None:
        if target.agent_id in bystander_ids:
            loaded.append(target.id)

    event.listen(Check, "load", _on_load)
    try:
        await AlertEvaluationCoreService.evaluate_check_result(db, judged)
    finally:
        event.remove(Check, "load", _on_load)

    assert loaded == [], (
        f"{len(loaded)} unrelated checks loaded to judge one {mode} result"
    )


# --- 2. a breach told with a sibling's result names that sibling -------------


async def test_breach_context_names_the_breaching_sibling(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """Replicate A,B on latency > 100. A's 900 ms is stored; B's 500 ms is
    judged first (A's result read only by the lateral, relations unloaded).
    The breach is told of A's 900 ms, by A."""
    owners = [make_agent(hostname="host-a"), make_agent(hostname="host-b")]
    db.add_all(owners)
    await db.flush()
    check = await _alerted_check(
        db,
        owners,
        "replicate",
        trigger_type="threshold",
        trigger_config={"metric": "latency_ms", "operator": ">", "value": 100},
    )
    at = utc_now() - timedelta(minutes=10)
    a = make_check_result(
        check_id=check.id, agent_id=owners[0].id, latency_ms=900.0, timestamp=at
    )
    b = make_check_result(
        check_id=check.id,
        agent_id=owners[1].id,
        latency_ms=500.0,
        timestamp=at + timedelta(seconds=5),
    )
    db.add_all([a, b])
    await db.flush()
    a_agent = owners[0].id

    judged = await _reload(db, b.id)
    await AlertEvaluationCoreService.evaluate_check_result(db, judged)
    await deliver_in(db)

    assert len(sent) == 1
    assert sent[0].latency_ms == 900.0
    assert sent[0].agent_id == a_agent
    assert sent[0].agent_name == "host-a"
