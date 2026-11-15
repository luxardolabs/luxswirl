"""A check's outage across its agents at the edges: silent agents, failover,
timeouts, the breaching agent, and the upgrade.

An agent that stopped reporting keeps the check as it last saw it; a late batch
from an agent the check moved off speaks for itself; a breach notification
carries the latency that breaches; and the upgrade's record of ended outages
never swallows a recovery or re-announces an ongoing outage.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.datetime_utils import utc_now
from app.notifications.providers.base import NotificationContext
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_check_result,
    make_notification_log,
    make_notification_provider,
)
from fixtures.outbox import deliver_in
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

THRESHOLD = {
    "trigger_type": "threshold",
    "trigger_config": {"metric": "latency_ms", "operator": ">", "value": 100},
}

Run = tuple[int, bool, float | None]

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "024_record_ended_outages.py"
)


def _migrate(connection: Any) -> None:
    spec = importlib.util.spec_from_file_location("rev024_adv", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


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

    async def run(self, *runs: Run, parent_ok: bool | None = None) -> None:
        for who, success, latency in runs:
            if parent_ok is not None:
                self.db.add(
                    make_check_result(
                        check_id=self.check.depends_on_check_id,
                        agent_id=self.agents[0].id,
                        success=parent_ok,
                        timestamp=self.at + timedelta(seconds=29),
                    )
                )
            await self.judge(await self.store(who, success, latency))

    async def last_row(self, key: str) -> None:
        then = utc_now() - timedelta(days=1)
        self.db.add(
            make_notification_log(
                alert_id=self.alert.id,
                notification_provider_id=self.provider.id,
                check_result_id=None,
                check_result_timestamp=then,
                check_id=self.check.id,
                notification_hash=key,
                sent_at=then,
            )
        )
        await self.db.flush()


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
    return Scene(db, made, check, alert_row, provider)


# --- 1. an assigned agent's failing latest result hidden by the lookback ------


@pytest.mark.parametrize(
    "healthy_runs", [8, 11], ids=["within-lookback", "past-lookback"]
)
async def test_silent_assigned_agent_keeps_check_down(
    db: AsyncSession, sent: list[NotificationContext], healthy_runs: int
) -> None:
    """Replicate A,B. A fails (announced) and then stops reporting while it is
    still an active agent matching the selector: its latest result is the
    failure, so by the ruling the check stays down. within-lookback is the
    control; past-lookback announces a recovery nobody had."""
    scene = await _scene(db, agents=2, mode="replicate")
    await scene.run((0, False, 10.0), *[(1, True, 10.0)] * healthy_runs)

    assert told(sent) == ["down"]


# --- 2. distribute stand-in's last batch after the owner returns -------------


@pytest.mark.parametrize("late_batch", [False, True], ids=["control", "late-batch"])
async def test_distribute_stand_in_late_failure(
    db: AsyncSession, sent: list[NotificationContext], late_batch: bool
) -> None:
    """Distribute over two agents. The first choice goes offline, the stand-in
    runs the check, finds it down (announced). The first choice comes back;
    the stand-in's last batch (another failure, measured before the hand-back,
    which `get_reportable_checks` accepts by design) is decided after it. The
    first choice then finds the check up. One outage: one down, one up."""
    scene = await _scene(db, agents=2, mode="distribute")
    first, second = sorted(
        (0, 1),
        key=lambda i: AgentAssignmentCoreService._hrw_score(
            scene.check, scene.agents[i]
        ),
        reverse=True,
    )
    # The first choice runs the check, healthy.
    await scene.run((first, True, 10.0))
    # It goes offline: the stand-in owns the check and finds it down.
    scene.agents[first].last_seen = utc_now() - timedelta(hours=2)
    await db.flush()
    await scene.run((second, False, 10.0))
    # The stand-in's last failure is stored; the first choice comes back.
    late = await scene.store(second, False, 10.0) if late_batch else None
    scene.agents[first].last_seen = utc_now()
    await db.flush()
    if late is not None:
        await scene.judge(late)
    # The first choice finds the check up.
    await scene.run((first, True, 10.0))

    assert told(sent) == ["down", "up"]


# --- 3. timeouts end a latency breach once another agent reports -------------


@pytest.mark.parametrize("rounds", [4, 6], ids=["within-lookback", "past-lookback"])
async def test_timeouts_do_not_end_a_breach_with_two_agents(
    db: AsyncSession, sent: list[NotificationContext], rounds: int
) -> None:
    """The one-agent rule (test_a_timeout_neither_ends_nor_starts_a_breach):
    a timeout has no latency and ends no breach. Replicate A,B: A breaches,
    then only times out while B stays fast. A's latest latency is still the
    breach; no recovery should be announced."""
    scene = await _scene(db, agents=2, mode="replicate", **THRESHOLD)
    await scene.run((0, True, 500.0), *[(0, False, None), (1, True, 50.0)] * rounds)

    assert told(sent) == ["down"]


# --- 4. a breach announced with a healthy agent's figures --------------------


@pytest.mark.parametrize(
    "parent_down_first", [False, True], ids=["control", "after-parent"]
)
async def test_breach_notification_carries_a_breaching_latency(
    db: AsyncSession, sent: list[NotificationContext], parent_down_first: bool
) -> None:
    """Replicate A,B with a parent. A breaches (500 ms > 100). When the
    parent was down at A's result, the breach is first announced at B's next
    (50 ms) result: the notification must still tell of a breaching latency."""
    scene = await _scene(db, agents=2, mode="replicate", with_parent=True, **THRESHOLD)
    await scene.run((0, True, 500.0), parent_ok=not parent_down_first)
    await scene.run((1, True, 50.0), parent_ok=True)

    assert told(sent) == ["down"]
    assert sent[0].latency_ms is not None and sent[0].latency_ms > 100


# --- 5. migration 024 swallows a queued recovery -----------------------------


@pytest.mark.parametrize("migrate", [False, True], ids=["control", "migrated"])
async def test_recovery_queued_across_upgrade_is_announced(
    db: AsyncSession, sent: list[NotificationContext], migrate: bool
) -> None:
    """A notify-on-recovery alert announced an outage; the check's success is
    stored and queued (the decision worker had not reached it when the server
    stopped for the upgrade). After the upgrade it must still be announced."""
    scene = await _scene(db, notify_on_recovery=True)
    await scene.last_row("status:down")
    queued = await scene.store(0, True, 10.0)
    if migrate:
        await db.run_sync(lambda s: _migrate(s.connection()))
    await scene.judge(queued)

    assert told(sent) == ["up"]


# --- 6. migration 024 on a replicate check still down -------------------------


@pytest.mark.parametrize("migrate", [False, True], ids=["control", "migrated"])
async def test_upgrade_does_not_reannounce_an_ongoing_outage(
    db: AsyncSession, sent: list[NotificationContext], migrate: bool
) -> None:
    """Replicate A,B. An outage of A was announced; B's newest result is a
    success while A still fails. After the upgrade A's next failure is the
    same outage: nothing new to tell."""
    scene = await _scene(db, agents=2, mode="replicate", notify_on_recovery=False)
    await scene.last_row("status:down")
    await scene.store(0, False, 10.0)
    await scene.store(1, True, 10.0)
    if migrate:
        await db.run_sync(lambda s: _migrate(s.connection()))
    await scene.run((0, False, 10.0))

    assert told(sent) == []
