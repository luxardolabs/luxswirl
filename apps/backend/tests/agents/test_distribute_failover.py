"""A distributed check runs on an online agent of its pool.

An agent that misses its heartbeats (`app.core.liveness`: 3 heartbeat
intervals plus 60 s) hands its distributed checks to the other agents of each
check's pool, and takes them back at its next heartbeat. Rendezvous hashing
moves only that agent's checks. The surviving agent learns of the checks it
took over from the heartbeat's config_version, any agent of the pool may
report a check's results, and the silence alerts measure a moved check from
the moment it moved.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import AgentStatus
from app.models.notification_log_model import NotificationLog
from app.notifications.providers.base import NotificationContext
from app.schemas.agent_schema import AgentHeartbeat
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.core.silence_alert_core_service import SilenceAlertCoreService
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

SELECTOR = {"tags": ["role:probe"]}
# heartbeat 60 s × 3 + 60 s grace
OFFLINE_AFTER = timedelta(seconds=240)

checks_for = AgentAssignmentCoreService.get_checks_for_agent


async def _pool(db: AsyncSession, n: int = 2) -> list[Agent]:
    agents = [make_agent(tags=["role:probe"], heartbeat_interval=60) for _ in range(n)]
    db.add_all(agents)
    await db.flush()
    return agents


async def _distributed(db: AsyncSession, owner: Agent, n: int = 1) -> list[Check]:
    checks = [
        make_check(
            agent_id=owner.id, assignment_mode="distribute", agent_selector=SELECTOR
        )
        for _ in range(n)
    ]
    for check in checks:
        check.created_at = utc_now() - timedelta(days=1)
    db.add_all(checks)
    await db.flush()
    return checks


def _first_choice(check: Check, pool: list[Agent]) -> tuple[Agent, Agent]:
    """The agent the hash picks for `check` from a pool of two, and the other."""
    first = AgentAssignmentCoreService.get_assigned_agent_for_check(check, pool)
    assert first is not None
    return first, next(a for a in pool if a.id != first.id)


async def _go_offline(db: AsyncSession, agent: Agent, ago: timedelta) -> None:
    agent.last_seen = utc_now() - ago
    await db.flush()


def _ids(checks: list[Check]) -> set[UUID]:
    return {c.id for c in checks}


# ── who runs it ──────────────────────────────────────────────────────────────


async def test_an_offline_agents_check_moves_to_an_online_one(
    db: AsyncSession,
) -> None:
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, survivor = _first_choice(check, pool)
    assert check.id in _ids(await checks_for(db, owner))  # control

    await _go_offline(db, owner, OFFLINE_AFTER + timedelta(seconds=30))

    assert check.id not in _ids(await checks_for(db, owner))
    assert check.id in _ids(await checks_for(db, survivor))


async def test_the_check_returns_at_the_agents_next_heartbeat(
    db: AsyncSession,
) -> None:
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, survivor = _first_choice(check, pool)
    await _go_offline(db, owner, timedelta(minutes=10))
    assert check.id in _ids(await checks_for(db, survivor))  # control

    owner.last_seen = utc_now()
    await db.flush()

    assert check.id in _ids(await checks_for(db, owner))
    assert check.id not in _ids(await checks_for(db, survivor))


async def test_a_missed_heartbeat_moves_nothing(db: AsyncSession) -> None:
    """Two missed heartbeats are inside the offline threshold."""
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, survivor = _first_choice(check, pool)

    await _go_offline(db, owner, timedelta(seconds=150))

    assert check.id in _ids(await checks_for(db, owner))
    assert check.id not in _ids(await checks_for(db, survivor))


async def test_only_the_offline_agents_checks_move(db: AsyncSession) -> None:
    pool = await _pool(db, 3)
    checks = await _distributed(db, pool[0], 30)
    before = {a.id: _ids(await checks_for(db, a)) for a in pool}
    gone = max(pool, key=lambda a: len(before[a.id]))
    assert before[gone.id]  # control: it had some to hand over

    await _go_offline(db, gone, timedelta(minutes=10))
    after = {a.id: _ids(await checks_for(db, a)) for a in pool}

    for agent in pool:
        if agent.id != gone.id:
            assert before[agent.id] <= after[agent.id]
    assert set().union(*after.values()) == _ids(checks)
    assert after[gone.id] == set()


async def test_with_every_agent_offline_the_check_stays_with_its_first_choice(
    db: AsyncSession,
) -> None:
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, other = _first_choice(check, pool)

    await _go_offline(db, owner, timedelta(minutes=10))
    await _go_offline(db, other, timedelta(minutes=20))

    assert check.id in _ids(await checks_for(db, owner))
    assert check.id not in _ids(await checks_for(db, other))


# ── the survivor learns of it ────────────────────────────────────────────────


async def _heartbeat_version(db: AsyncSession, agent: Agent) -> str | None:
    response = await AgentCoreService.process_heartbeat(
        db,
        agent.id,
        AgentHeartbeat(
            agent_id=agent.id, timestamp=utc_now(), status=AgentStatus.ONLINE
        ),
        using_agent_key=True,
    )
    return response.config_version


async def test_the_heartbeat_tells_the_survivor_its_checks_changed(
    db: AsyncSession,
) -> None:
    """The agent re-fetches its checks only when config_version changes, so a
    check it takes over must change it, and so must the check moving back."""
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, survivor = _first_choice(check, pool)
    before = await _heartbeat_version(db, survivor)

    await _go_offline(db, owner, timedelta(minutes=10))
    during = await _heartbeat_version(db, survivor)

    owner.last_seen = utc_now()
    await db.flush()
    after = await _heartbeat_version(db, survivor)

    assert during != before
    assert after != during
    assert after == before


async def test_the_version_is_stable_while_nothing_moves(db: AsyncSession) -> None:
    pool = await _pool(db)
    await _distributed(db, pool[0], 5)

    first = await _heartbeat_version(db, pool[1])
    second = await _heartbeat_version(db, pool[1])

    assert first == second
    assert first is None or len(first) <= 50  # fits agents.config_version


# ── who may report it ────────────────────────────────────────────────────────


def _report(agent_id: UUID, check_id: UUID, ago: timedelta) -> AgentReportRequest:
    return AgentReportRequest(
        agent_id=agent_id,
        checks=[
            CheckResultCreate(
                timestamp=utc_now() - ago,
                success=True,
                latency_ms=5.0,
                check_id=str(check_id),
                display_name="probe",
                check_type="ping",
                target="127.0.0.1",
            )
        ],
    )


async def test_the_owners_buffered_results_are_kept_before_its_heartbeat(
    db: AsyncSession,
) -> None:
    """An agent back from a network outage replays what it measured before its
    first heartbeat lands; the check has moved, but those results are its."""
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, _ = _first_choice(check, pool)
    await _go_offline(db, owner, timedelta(minutes=10))

    taken = await CheckResultCoreService.process_agent_report(
        db, _report(owner.id, check.id, timedelta(minutes=9))
    )

    assert taken["results_processed"] == 1


async def test_the_stand_ins_last_batch_is_kept_after_the_owner_returns(
    db: AsyncSession,
) -> None:
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    _, survivor = _first_choice(check, pool)

    taken = await CheckResultCoreService.process_agent_report(
        db, _report(survivor.id, check.id, timedelta(seconds=20))
    )

    assert taken["results_processed"] == 1


async def test_an_agent_outside_the_pool_still_may_not_report(
    db: AsyncSession,
) -> None:
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    outsider = make_agent(tags=["role:db"])
    pending = make_agent(tags=["role:probe"], approval_status="pending")
    db.add_all([outsider, pending])
    await db.flush()

    for agent in (outsider, pending):
        taken = await CheckResultCoreService.process_agent_report(
            db, _report(agent.id, check.id, timedelta(seconds=5))
        )
        assert taken["results_processed"] == 0
        assert not await AgentAssignmentCoreService.may_report_for_check(
            db, agent.id, check.id
        )
    assert await AgentAssignmentCoreService.may_report_for_check(  # control
        db, pool[1].id, check.id
    )


# ── the silence alerts ───────────────────────────────────────────────────────


@pytest.fixture
def sent() -> Iterator[list[NotificationContext]]:
    contexts: list[NotificationContext] = []

    async def _send(self, context: NotificationContext) -> bool:
        contexts.append(context)
        return True

    with patch(
        "app.notifications.providers.webhook.WebhookNotificationProvider.send",
        _send,
    ):
        yield contexts


async def _alert(db: AsyncSession, trigger_type: str, check: Check):
    alert = make_alert(
        trigger_type=trigger_type, trigger_config={"missed_intervals": 3}
    )
    provider = make_notification_provider()
    db.add_all([alert, provider])
    await db.flush()
    db.add_all(
        [
            make_alert_notification_mapping(
                alert_id=alert.id, notification_provider_id=provider.id
            ),
            make_alert_check_mapping(alert_id=alert.id, check_id=check.id),
        ]
    )
    await db.flush()
    return alert


async def _rows(db: AsyncSession, alert) -> list[NotificationLog]:
    result = await db.execute(
        select(NotificationLog).where(NotificationLog.alert_id == alert.id)
    )
    return list(result.scalars().all())


async def test_a_check_that_just_moved_is_not_yet_silent(
    db: AsyncSession, sent
) -> None:
    """The new agent has had no time to report it: no notice, not even a
    suppressed one."""
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, _ = _first_choice(check, pool)
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=owner.id,
            timestamp=utc_now() - timedelta(minutes=10),
        )
    )
    await _go_offline(db, owner, OFFLINE_AFTER + timedelta(seconds=10))
    alert = await _alert(db, "no_data", check)

    await SilenceAlertCoreService.evaluate(db)

    assert sent == []
    assert await _rows(db, alert) == []


async def test_a_moved_check_its_new_agent_never_reports_notifies(
    db: AsyncSession, sent
) -> None:
    """Its silence is the online agent's now, so it is sent, not suppressed."""
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, _ = _first_choice(check, pool)
    await _go_offline(db, owner, timedelta(minutes=30))
    alert = await _alert(db, "no_data", check)

    await SilenceAlertCoreService.evaluate(db)

    assert [c.status_label for c in sent] == ["NO DATA"]
    assert [r.status for r in await _rows(db, alert)] != ["suppressed"]


async def test_an_offline_agent_is_watched_after_its_checks_moved(
    db: AsyncSession, sent
) -> None:
    pool = await _pool(db)
    (check,) = await _distributed(db, pool[0])
    owner, _ = _first_choice(check, pool)
    await _go_offline(db, owner, timedelta(minutes=30))
    await _alert(db, "agent_offline", check)

    await SilenceAlertCoreService.evaluate(db)

    assert [(c.status_label, c.agent_id) for c in sent] == [("OFFLINE", owner.id)]
