"""An alert announces each outage of a check, and its end, once.

A check is down while any agent that currently runs it has a failing latest
result, and up once every such agent's latest result succeeds; a latency
threshold is breached while any such agent's latest latency breaches it, and
clear once every one is on the safe side (a timeout, which has no latency,
changes neither). A recovery tells of the end of an outage the alert
announced; a recovery that is not announced (recovery notifications off) is
still recorded, so the next outage is a new one.
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
REPEATED = {
    "trigger_type": "repeated_failure",
    "trigger_config": {"on_status": ["error"], "consecutive_failures": 2},
}

# (agent index, success, latency): one run of the check.
Run = tuple[int, bool, float | None]
DOWN: Run = (0, False, 10.0)
UP: Run = (0, True, 10.0)


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


class Scene:
    """A check, its agents, an alert with a webhook, and a clock."""

    def __init__(self, db: AsyncSession, agents: list[Any], check: Any) -> None:
        self.db = db
        self.agents = agents
        self.check = check
        self.at = utc_now() - timedelta(hours=1)

    async def run(self, *runs: Run, parent_ok: bool | None = None) -> None:
        """Store each run 30 s after the last, then decide and deliver it."""
        for who, success, latency in runs:
            self.at += timedelta(seconds=30)
            if parent_ok is not None:
                self.db.add(
                    make_check_result(
                        check_id=self.check.depends_on_check_id,
                        agent_id=self.agents[0].id,
                        success=parent_ok,
                        timestamp=self.at - timedelta(seconds=1),
                    )
                )
            result = make_check_result(
                check_id=self.check.id,
                agent_id=self.agents[who].id,
                success=success,
                latency_ms=latency,
                timestamp=self.at,
            )
            self.db.add(result)
            await self.db.flush()
            await AlertEvaluationCoreService.evaluate_check_result(self.db, result)
            await deliver_in(self.db)


async def _scene(
    db: AsyncSession,
    *,
    agents: int = 1,
    mode: str = "manual",
    with_parent: bool = False,
    **alert: Any,
) -> Scene:
    made = [make_agent() for _ in range(agents)]
    db.add_all(made)
    await db.flush()
    parent = None
    if with_parent:
        parent = make_check(agent_id=made[0].id)
        db.add(parent)
        await db.flush()
    check = make_check(
        agent_id=made[0].id,
        assignment_mode=mode,
        agent_selector=(
            {"agent_ids": [a.agent_name for a in made]} if mode != "manual" else None
        ),
        depends_on_check_id=parent.id if parent else None,
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
    return Scene(db, made, check)


def told(sent: list[NotificationContext]) -> list[str]:
    """Each notification sent: `down` for an outage, `up` for its end."""
    return ["up" if c.is_recovery else "down" for c in sent]


# --- one agent ---------------------------------------------------------------


@pytest.mark.parametrize("alert", [{}, REPEATED], ids=["status", "repeated"])
@pytest.mark.parametrize("recovery", [True, False])
async def test_a_second_outage_is_announced(
    db: AsyncSession,
    sent: list[NotificationContext],
    alert: dict[str, Any],
    recovery: bool,
) -> None:
    scene = await _scene(db, notify_on_recovery=recovery, **alert)
    await scene.run(DOWN, DOWN, UP, DOWN, DOWN)

    assert told(sent) == (["down", "up", "down"] if recovery else ["down", "down"])


async def test_a_steady_outage_is_announced_once(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db)
    await scene.run(DOWN, DOWN, DOWN, DOWN)

    assert told(sent) == ["down"]


@pytest.mark.parametrize("recovery", [True, False])
async def test_a_threshold_breach_and_its_end(
    db: AsyncSession, sent: list[NotificationContext], recovery: bool
) -> None:
    """The check succeeds throughout: only the latency breaches."""
    scene = await _scene(db, notify_on_recovery=recovery, **THRESHOLD)
    await scene.run((0, True, 500.0), (0, True, 50.0), (0, True, 500.0))

    assert told(sent) == (["down", "up", "down"] if recovery else ["down", "down"])


async def test_a_timeout_neither_ends_nor_starts_a_breach(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, **THRESHOLD)
    await scene.run((0, True, 500.0), (0, False, None), (0, True, 500.0))

    assert told(sent) == ["down"]


async def test_a_breach_ends_after_any_number_of_timeouts(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, **THRESHOLD)
    await scene.run((0, True, 500.0), *[(0, False, None)] * 60, (0, True, 50.0))

    assert told(sent) == ["down", "up"]


async def test_an_outage_seen_only_under_a_parent_outage_has_no_recovery(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, with_parent=True)
    await scene.run(DOWN, parent_ok=False)
    await scene.run(UP, parent_ok=True)

    assert told(sent) == []


async def test_an_announced_outage_ends_after_a_parent_outage(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, with_parent=True)
    await scene.run(DOWN, parent_ok=True)
    await scene.run(DOWN, parent_ok=False)
    await scene.run(UP, parent_ok=True)

    assert told(sent) == ["down", "up"]


# --- several agents ----------------------------------------------------------


@pytest.mark.parametrize("recovery", [True, False])
async def test_one_agent_down_while_another_is_up_is_one_outage(
    db: AsyncSession, sent: list[NotificationContext], recovery: bool
) -> None:
    scene = await _scene(db, agents=2, mode="replicate", notify_on_recovery=recovery)
    await scene.run(*[(0, False, 10.0), (1, True, 10.0)] * 4)

    assert told(sent) == ["down"]


async def test_the_outage_ends_when_every_agent_is_up(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, agents=2, mode="replicate")
    await scene.run((0, False, 10.0), (1, True, 10.0), (0, True, 10.0))

    assert told(sent) == ["down", "up"]


async def test_one_slow_agent_is_one_breach(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, agents=2, mode="replicate", **THRESHOLD)
    await scene.run(*[(0, True, 500.0), (1, True, 50.0)] * 4)

    assert told(sent) == ["down"]


async def test_consecutive_failures_are_counted_per_agent(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    scene = await _scene(db, agents=2, mode="replicate", **REPEATED)
    await scene.run((0, False, 10.0), (1, True, 10.0), (0, False, 10.0))

    assert told(sent) == ["down"]


async def test_a_check_moved_to_another_agent_announces_its_next_outage(
    db: AsyncSession, sent: list[NotificationContext]
) -> None:
    """Recovery notifications off: the old agent's last failure does not keep
    the check down once another agent runs it."""
    scene = await _scene(db, agents=2, notify_on_recovery=False)
    await scene.run(DOWN)
    scene.check.agent_id = scene.agents[1].id
    await db.flush()
    await scene.run((1, True, 10.0), (1, True, 10.0), (1, False, 10.0))

    assert told(sent) == ["down", "down"]
