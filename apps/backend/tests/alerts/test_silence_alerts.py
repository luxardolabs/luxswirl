"""The silence alerts fire on the absence of data.

A no_data alert notifies when a check that an active agent is to run has
reported nothing for `missed_intervals` of its interval (plus a minute's
grace); an agent_offline alert notifies when an active agent's heartbeat is
that late. Each silence is one episode: one notification, the alert's resend
rules, then a recovery when data comes back. A check whose agents are all
offline is the agent's silence, so its no_data row is suppressed, not sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch

import pytest
from app.core.datetime_utils import utc_now
from app.crud.alert_crud import AlertCRUD
from app.db.defaults.scheduler_templates import DEFAULT_JOB_CONFIGURATIONS
from app.models.agent_model import Agent
from app.models.alert_model import Alert
from app.models.check_model import Check
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.services.core.scheduler_core_service import SchedulerCoreService
from app.services.core.silence_alert_core_service import (
    AGENT_OFFLINE,
    CHECK_REPORTING,
    CHECK_SILENT,
    SilenceAlertCoreService,
)
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

# interval 60 s × 3 missed + 60 s grace
THRESHOLD = timedelta(seconds=240)


@pytest.fixture
def sent() -> Iterator[list[NotificationContext]]:
    """Every context a provider was asked to deliver (the network is stubbed)."""
    contexts: list[NotificationContext] = []

    async def _send(self, context: NotificationContext) -> bool:
        contexts.append(context)
        return True

    with patch(
        "app.notifications.providers.webhook.WebhookNotificationProvider.send",
        _send,
    ):
        yield contexts


async def _alert(
    db: AsyncSession, trigger_type: str, checks: list[Check], **overrides
) -> Alert:
    alert = make_alert(
        trigger_type=trigger_type,
        trigger_config={"missed_intervals": 3},
        **overrides,
    )
    provider = make_notification_provider()
    db.add_all([alert, provider])
    await db.flush()
    db.add(
        make_alert_notification_mapping(
            alert_id=alert.id, notification_provider_id=provider.id
        )
    )
    db.add_all(
        make_alert_check_mapping(alert_id=alert.id, check_id=c.id) for c in checks
    )
    await db.flush()
    return alert


async def _check(
    db: AsyncSession, agent: Agent, *, last_result_ago: timedelta | None, **kw
) -> Check:
    check = make_check(agent_id=agent.id, **kw)
    check.created_at = utc_now() - timedelta(days=1)
    db.add(check)
    await db.flush()
    if last_result_ago is not None:
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=utc_now() - last_result_ago,
            )
        )
        await db.flush()
    return check


async def _agent(db: AsyncSession, **overrides) -> Agent:
    agent = make_agent(**overrides)
    db.add(agent)
    await db.flush()
    return agent


async def _rows(db: AsyncSession, alert: Alert) -> list[NotificationLog]:
    result = await db.execute(
        select(NotificationLog)
        .where(NotificationLog.alert_id == alert.id)
        .order_by(NotificationLog.sent_at)
    )
    return list(result.scalars().all())


# ── no_data ──────────────────────────────────────────────────────────────────


async def test_a_silent_check_notifies_once(db: AsyncSession, sent) -> None:
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=THRESHOLD + timedelta(minutes=5))
    alert = await _alert(db, "no_data", [check])

    await SilenceAlertCoreService.evaluate(db)
    await SilenceAlertCoreService.evaluate(db)

    assert len(sent) == 1
    assert sent[0].check_name == check.display_name
    assert sent[0].status_label == "NO DATA"
    assert (sent[0].error_message or "").startswith("No results for 9 min")
    rows = await _rows(db, alert)
    assert [r.notification_hash for r in rows] == [CHECK_SILENT]
    assert rows[0].check_id == check.id
    assert rows[0].check_result_id is None


async def test_a_check_reporting_on_time_does_not_notify(
    db: AsyncSession, sent
) -> None:
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=THRESHOLD - timedelta(seconds=30))
    await _alert(db, "no_data", [check])

    counts = await SilenceAlertCoreService.evaluate(db)

    assert sent == []
    assert counts.silent_checks == 0


async def test_missed_intervals_scales_the_threshold(db: AsyncSession, sent) -> None:
    """Ten minutes late is silent at 3 missed intervals, not at 20."""
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(minutes=10))
    alert = await _alert(db, "no_data", [check])
    alert.trigger_config = {"missed_intervals": 20}
    await db.flush()

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []


async def test_data_resuming_sends_a_recovery_and_ends_the_episode(
    db: AsyncSession, sent
) -> None:
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=THRESHOLD + timedelta(minutes=5))
    alert = await _alert(db, "no_data", [check])
    await SilenceAlertCoreService.evaluate(db)

    db.add(make_check_result(check_id=check.id, agent_id=agent.id))
    await db.flush()
    await SilenceAlertCoreService.evaluate(db)
    await SilenceAlertCoreService.evaluate(db)

    assert [c.is_recovery for c in sent] == [False, True]
    assert sent[1].status_label == "UP"
    rows = await _rows(db, alert)
    assert [r.notification_hash for r in rows] == [CHECK_SILENT, CHECK_REPORTING]


async def test_a_later_silence_is_a_new_episode(db: AsyncSession, sent) -> None:
    """Without a recovery row (notify_on_recovery off), a result between two
    silences still ends the first, so the second one notifies."""
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=THRESHOLD + timedelta(minutes=5))
    alert = await _alert(db, "no_data", [check], notify_on_recovery=False)
    await SilenceAlertCoreService.evaluate(db)
    (first,) = await _rows(db, alert)
    first.sent_at = utc_now() - timedelta(hours=2)
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            timestamp=utc_now() - timedelta(hours=1),
        )
    )
    await db.flush()

    await SilenceAlertCoreService.evaluate(db)

    assert [c.is_recovery for c in sent] == [False, False]
    assert len(await _rows(db, alert)) == 2


async def test_a_silence_resends_after_the_resend_interval(
    db: AsyncSession, sent
) -> None:
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(hours=3))
    alert = await _alert(db, "no_data", [check], resend_interval_minutes=30)
    await SilenceAlertCoreService.evaluate(db)
    (first,) = await _rows(db, alert)
    first.sent_at = utc_now() - timedelta(minutes=31)
    await db.flush()

    await SilenceAlertCoreService.evaluate(db)

    rows = await _rows(db, alert)
    assert [(r.is_resend, r.resend_count) for r in rows] == [(False, 0), (True, 1)]
    assert len(sent) == 2


async def test_a_check_that_never_reported_counts_from_its_creation(
    db: AsyncSession, sent
) -> None:
    agent = await _agent(db)
    old = await _check(db, agent, last_result_ago=None)
    new = make_check(agent_id=agent.id)
    db.add(new)
    await db.flush()
    await _alert(db, "no_data", [old, new])

    await SilenceAlertCoreService.evaluate(db)

    assert [c.check_name for c in sent] == [old.display_name]
    assert "since it was created" in (sent[0].error_message or "")


@pytest.mark.parametrize("status", ["paused", "disabled", "pending", "rejected"])
async def test_a_check_no_active_agent_runs_is_not_expected_to_report(
    db: AsyncSession, sent, status: str
) -> None:
    agent = await _agent(db, approval_status=status)
    check = await _check(db, agent, last_result_ago=timedelta(hours=3))
    await _alert(db, "no_data", [check])

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []


async def test_a_disabled_check_is_not_expected_to_report(
    db: AsyncSession, sent
) -> None:
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(hours=3), enabled=False)
    await _alert(db, "no_data", [check])

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []


async def test_a_snoozed_check_does_not_notify(db: AsyncSession, sent) -> None:
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(hours=3))
    alert = await _alert(db, "no_data", [check])
    mapping = await AlertCRUD.get_check_mapping(db, alert.id, check.id)
    assert mapping is not None
    mapping.snoozed_until = utc_now() + timedelta(hours=1)
    await db.flush()

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []


async def test_a_replicate_check_is_silent_only_when_no_agent_reports(
    db: AsyncSession, sent
) -> None:
    """One result from any of the agents running it keeps the check reporting."""
    a = await _agent(db, tags=["role:probe"])
    b = await _agent(db, tags=["role:probe"])
    check = await _check(
        db,
        a,
        last_result_ago=None,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:probe"]},
    )
    db.add(make_check_result(check_id=check.id, agent_id=b.id))
    await db.flush()
    await _alert(db, "no_data", [check])

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []


async def test_a_result_does_not_evaluate_a_no_data_alert(
    db: AsyncSession, sent
) -> None:
    """The per-result path skips the silence triggers: the scheduled pass
    owns them, so a result neither fires them nor logs an unknown trigger."""
    agent = await _agent(db)
    check = await _check(db, agent, last_result_ago=None)
    alert = await _alert(db, "no_data", [check])
    result = make_check_result(check_id=check.id, agent_id=agent.id, success=False)
    db.add(result)
    await db.flush()

    await AlertEvaluationCoreService.evaluate_check_result(db, result)

    assert sent == []
    assert await _rows(db, alert) == []


# ── agent_offline ────────────────────────────────────────────────────────────


async def _offline_agent(db: AsyncSession) -> Agent:
    # heartbeat 5 s × 3 + 60 s grace = 75 s
    return await _agent(
        db, heartbeat_interval=5, last_seen=utc_now() - timedelta(minutes=10)
    )


async def test_an_offline_agent_notifies_once(db: AsyncSession, sent) -> None:
    agent = await _offline_agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(minutes=10))
    alert = await _alert(db, "agent_offline", [check])

    await SilenceAlertCoreService.evaluate(db)
    await SilenceAlertCoreService.evaluate(db)

    assert len(sent) == 1
    assert sent[0].status_label == "OFFLINE"
    assert sent[0].agent_id == agent.id
    rows = await _rows(db, alert)
    assert [(r.notification_hash, r.agent_id, r.check_id) for r in rows] == [
        (AGENT_OFFLINE, agent.id, None)
    ]


async def test_an_online_agent_does_not_notify(db: AsyncSession, sent) -> None:
    agent = await _agent(db, heartbeat_interval=5)
    check = await _check(db, agent, last_result_ago=timedelta(seconds=10))
    await _alert(db, "agent_offline", [check])

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []


async def test_a_heartbeat_after_the_outage_sends_a_recovery(
    db: AsyncSession, sent
) -> None:
    agent = await _offline_agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(minutes=10))
    await _alert(db, "agent_offline", [check])
    await SilenceAlertCoreService.evaluate(db)

    agent.last_seen = utc_now()
    await db.flush()
    await SilenceAlertCoreService.evaluate(db)

    assert [(c.is_recovery, c.status_label) for c in sent] == [
        (False, "OFFLINE"),
        (True, "UP"),
    ]


async def test_an_offline_agents_checks_are_suppressed_not_sent(
    db: AsyncSession, sent
) -> None:
    """A dead agent with many checks sends one agent_offline notification;
    each check's no_data row is written suppressed, once."""
    agent = await _offline_agent(db)
    checks = [
        await _check(db, agent, last_result_ago=timedelta(minutes=10)) for _ in range(5)
    ]
    no_data = await _alert(db, "no_data", checks)
    await _alert(db, "agent_offline", checks)

    await SilenceAlertCoreService.evaluate(db)
    await SilenceAlertCoreService.evaluate(db)

    assert [c.status_label for c in sent] == ["OFFLINE"]
    rows = await _rows(db, no_data)
    assert len(rows) == 5
    assert {r.status for r in rows} == {"suppressed"}
    assert {r.check_id for r in rows} == {c.id for c in checks}


async def test_a_suppressed_silence_sends_no_recovery(db: AsyncSession, sent) -> None:
    agent = await _offline_agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(minutes=10))
    no_data = await _alert(db, "no_data", [check])
    await SilenceAlertCoreService.evaluate(db)

    agent.last_seen = utc_now()
    db.add(make_check_result(check_id=check.id, agent_id=agent.id))
    await db.flush()
    await SilenceAlertCoreService.evaluate(db)

    assert sent == []
    assert [r.status for r in await _rows(db, no_data)] == ["suppressed"]


async def test_a_check_still_silent_once_its_agent_returns_notifies(
    db: AsyncSession, sent
) -> None:
    """Suppression lasts only while the agent is offline: an agent back online
    whose check still reports nothing is the check's own silence."""
    agent = await _offline_agent(db)
    check = await _check(db, agent, last_result_ago=timedelta(minutes=10))
    await _alert(db, "no_data", [check])
    await SilenceAlertCoreService.evaluate(db)

    agent.last_seen = utc_now()
    await db.flush()
    await SilenceAlertCoreService.evaluate(db)

    assert [c.status_label for c in sent] == ["NO DATA"]


async def test_the_scheduler_runs_the_silence_pass() -> None:
    """The job is registered under the name its seeded configuration calls."""
    (config,) = [
        c
        for c in DEFAULT_JOB_CONFIGURATIONS
        if c["job_key"] == "evaluate_silence_alerts"
    ]
    assert config["enabled"] is True
    assert config["interval_seconds"] == 60
    assert config["function_name"] in SchedulerCoreService().job_functions
