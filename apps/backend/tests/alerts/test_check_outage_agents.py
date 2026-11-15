"""A check's outage across its agents: providers, recoveries, agents leaving,
and what reading the agents costs.

The end of an outage is recorded even while the alert has no provider, so the
next outage is announced; a latency recovery tells of a clear latency; an agent
that leaves the check stops holding it down; and judging a result reads the
check's agents in a fixed number of statements, however many there are.
"""

from __future__ import annotations

from collections import Counter
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
from sqlalchemy import event
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


def told(sent: list[NotificationContext]) -> list[str]:
    return ["up" if c.is_recovery else "down" for c in sent]


class Scene:
    def __init__(
        self, db: AsyncSession, agents: list[Any], check: Any, alert: Any, provider: Any
    ) -> None:
        self.db = db
        self.agents = agents
        self.check = check
        self.alert = alert
        self.provider = provider
        self.at = utc_now() - timedelta(hours=1)

    async def store(self, who: int, success: bool, latency: float | None) -> Any:
        self.at += timedelta(seconds=30)
        result = make_check_result(
            check_id=self.check.id,
            agent_id=self.agents[who].id,
            success=success,
            latency_ms=latency,
            timestamp=self.at,
        )
        self.db.add(result)
        await self.db.flush()
        return result

    async def judge(self, result: Any) -> None:
        await AlertEvaluationCoreService.evaluate_check_result(self.db, result)
        await deliver_in(self.db)

    async def run(self, *runs: tuple[int, bool, float | None]) -> None:
        for who, success, latency in runs:
            await self.judge(await self.store(who, success, latency))


async def _scene(
    db: AsyncSession, *, agents: int = 1, mode: str = "manual", **alert: Any
) -> Scene:
    made = [make_agent() for _ in range(agents)]
    db.add_all(made)
    await db.flush()
    check = make_check(
        agent_id=made[0].id,
        assignment_mode=mode,
        agent_selector=(
            {"agent_ids": [a.agent_name for a in made]} if mode != "manual" else None
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
    return Scene(db, made, check, alert_row, provider)


# --- A. the end of an outage is lost while the alert has no live provider ----


@pytest.mark.parametrize("notify_on_recovery", [False, True], ids=["no-rec", "rec"])
@pytest.mark.parametrize(
    "provider_gone", [False, True], ids=["control", "provider-gone"]
)
async def test_next_outage_announced_after_recovery_without_provider(
    db: AsyncSession,
    sent: list[NotificationContext],
    provider_gone: bool,
    notify_on_recovery: bool,
) -> None:
    """An outage is announced. The alert's provider is deleted; the check
    comes back up while the alert has no live provider; a new provider is
    put on the alert; the check goes down again. That second outage is a new
    one and must be announced."""
    scene = await _scene(db, notify_on_recovery=notify_on_recovery)
    await scene.run((0, False, 10.0))
    assert told(sent) == ["down"]
    if provider_gone:
        scene.provider.deleted_at = utc_now()
        await db.flush()
    await scene.run((0, True, 10.0), (0, True, 10.0))
    if provider_gone:
        fresh = make_notification_provider()
        db.add(fresh)
        await db.flush()
        db.add(
            make_alert_notification_mapping(
                alert_id=scene.alert.id, notification_provider_id=fresh.id
            )
        )
        await db.flush()
    sent.clear()
    await scene.run((0, False, 10.0))

    assert told(sent) == ["down"]


# --- B. a latency recovery told with a timed-out result ----------------------


@pytest.mark.parametrize("timeout", [False, True], ids=["control", "timeout"])
async def test_latency_recovery_carries_a_clear_latency(
    db: AsyncSession, sent: list[NotificationContext], timeout: bool
) -> None:
    """Replicate A,B on a latency threshold that notifies on recovery. B is
    fast (50 ms), A breaches (500 ms): announced. A leaves the selector. B's
    next result ends the breach. When that result is a timeout (no latency,
    failed) the recovery is told with it: a 'recovered' notification with a
    failed status and no latency."""
    scene = await _scene(
        db, agents=2, mode="replicate", notify_on_recovery=True, **THRESHOLD
    )
    await scene.run((1, True, 50.0), (0, True, 500.0))
    assert told(sent) == ["down"]
    scene.check.agent_selector = {"agent_ids": [scene.agents[1].agent_name]}
    await db.flush()
    await scene.run((1, False, None) if timeout else (1, True, 40.0))

    assert told(sent) == ["down", "up"]
    recovery = sent[-1]
    assert recovery.latency_ms is not None and recovery.latency_ms <= 100
    assert recovery.success


# --- C. probes: an agent that no longer runs the check -----------------------


@pytest.mark.parametrize("how", ["control", "retagged", "paused", "archived"])
async def test_failing_agent_that_left_lets_check_recover(
    db: AsyncSession, sent: list[NotificationContext], how: str
) -> None:
    """Replicate A,B. A fails (announced). A then stops running the check:
    it leaves the selector, is paused or archived. B's next success is the
    check up across its agents: the recovery is told. Control: A itself
    recovers."""
    scene = await _scene(db, agents=2, mode="replicate", notify_on_recovery=True)
    await scene.run((1, True, 10.0), (0, False, 10.0))
    if how == "retagged":
        scene.check.agent_selector = {"agent_ids": [scene.agents[1].agent_name]}
    elif how in ("paused", "archived"):
        scene.agents[0].approval_status = how
    await db.flush()
    await scene.run((0, True, 10.0) if how == "control" else (1, True, 10.0))

    assert told(sent) == ["down", "up"]


# --- D. per-result read cost grows with the agents of a replicate check ------


@pytest.mark.parametrize("agents", [2, 50], ids=["two-agents", "fifty-agents"])
async def test_view_reads_do_not_grow_with_agents(
    db: AsyncSession, sent: list[NotificationContext], agents: int
) -> None:
    """One result of a replicate check, judged by a status alert: the
    statements the evaluation runs. Each agent costs two reads (latest, latest
    with a latency), so a 50-agent check reporting every 30 s costs about 100
    statements per result, 10,000 a minute for that one check."""
    scene = await _scene(db, agents=agents, mode="replicate")
    for who in range(agents):
        await scene.store(who, True, 10.0)
    result = await scene.store(0, True, 10.0)

    conn = (await db.connection()).sync_connection
    assert conn is not None
    statements: list[str] = []

    def _count(*args: Any) -> None:
        statements.append(args[2])

    event.listen(conn, "before_cursor_execute", _count)
    try:
        await AlertEvaluationCoreService.evaluate_check_result(db, result)
    finally:
        event.remove(conn, "before_cursor_execute", _count)

    shapes = Counter(" ".join(s.split())[:90] for s in statements)
    assert len(statements) <= 40, (
        f"{len(statements)} statements for {agents} agents: {shapes.most_common(8)}"
    )
