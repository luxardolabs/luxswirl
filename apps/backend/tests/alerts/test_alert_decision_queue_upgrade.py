"""The alert decision queue across the upgrade that adds it, and over time.

Migration 022 starts each check's decision cursor at its newest stored
result: the results stored before the upgrade had their alerts decided by the
reports that stored them, so one sent again is stale, while a result an
agent buffered while the server was down for the upgrade (older than the
upgrade, newer than anything stored) is still decided. The suite's schema is
already at head, so these run the revision itself, down and up again, over
data made before it.

The decision pass reads one row per check with queued results, however many
each has queued, and a check no enabled alert evaluates any longer has its
queue drained, undecided, at its next pass.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.crud.alert_decision_crud import AlertDecisionCRUD
from app.main import app
from app.models.alert_decision_model import AlertDecisionCursor, AlertDecisionQueue
from app.models.alert_model import Alert
from app.models.notification_log_model import NotificationLog
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_decision_core_service import AlertDecisionCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_check_result,
    make_notification_provider,
)
from fixtures.outbox import decide_committed
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "022_alert_decision_queue.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev022", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def _upgrade(worker_db: Any) -> None:
    """Run migration 022 again over what is stored now: its tables dropped
    and made anew, its cursors seeded, in one committed transaction."""
    async with worker_db() as s:
        await s.run_sync(lambda sync: _run(sync.connection(), "downgrade"))
        await s.run_sync(lambda sync: _run(sync.connection(), "upgrade"))
        await s.commit()


@pytest.fixture
def agent_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()


async def _seed(worker_db: Any) -> dict[str, Any]:
    """An approved agent with one check, one alert on it with a provider."""
    async with worker_db() as s:
        agent = make_agent(approval_status="pending")
        s.add(agent)
        await s.flush()
        _, key = await AgentCoreService.approve_agent(s, agent.id, actor_is_admin=True)
        check = make_check(agent_id=agent.id, display_name="svc")
        provider = make_notification_provider()
        made = make_alert()
        s.add_all([check, provider, made])
        await s.flush()
        s.add_all(
            [
                make_alert_check_mapping(alert_id=made.id, check_id=check.id),
                make_alert_notification_mapping(
                    alert_id=made.id, notification_provider_id=provider.id
                ),
            ]
        )
        await s.commit()
    return {"agent": agent.id, "key": key, "alert": made.id, "check": check.id}


def _wire(check_id: UUID, at: datetime, *, success: bool) -> dict[str, Any]:
    return {
        "check_id": str(check_id),
        "display_name": "probe",
        "check_type": "ping",
        "target": "192.0.2.10",
        "timestamp": at.isoformat(),
        "success": success,
        "error": None if success else "down",
        "latency_ms": 5.0,
    }


async def _post(ids: dict[str, Any], at: datetime, *, success: bool) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=None
    ) as client:
        response = await client.post(
            "/api/v1/agent/reports",
            json={
                "agent_id": str(ids["agent"]),
                "checks": [_wire(ids["check"], at, success=success)],
            },
            headers={"Authorization": f"Bearer {ids['key']}"},
        )
    assert response.status_code == 202, response.text


async def _states(worker_db: Any, alert_id: UUID) -> list[str | None]:
    async with worker_db() as s:
        rows = await s.execute(
            select(NotificationLog.notification_hash)
            .where(NotificationLog.alert_id == alert_id)
            .order_by(NotificationLog.decision_seq)
        )
        return list(rows.scalars().all())


async def test_recovery_buffered_across_the_upgrade_is_notified(
    worker_db: Any, agent_auth: None
) -> None:
    """The check went down and the operator was told; the server was then
    upgraded while the agent buffered the check's recovery. The replayed
    recovery is notified, and the next outage is notified as a new one."""
    ids = await _seed(worker_db)
    start = utc_now()
    await _post(ids, start - timedelta(minutes=10), success=False)
    await decide_committed()

    await _upgrade(worker_db)

    await _post(ids, start - timedelta(minutes=5), success=True)
    await decide_committed()
    await _post(ids, utc_now(), success=True)
    await decide_committed()
    await _post(ids, utc_now() + timedelta(milliseconds=50), success=False)
    await decide_committed()

    assert await _states(worker_db, ids["alert"]) == [
        "status:down",
        "status:up",
        "status:down",
    ]


async def test_a_result_stored_before_the_upgrade_is_not_decided_again(
    worker_db: Any, agent_auth: None
) -> None:
    """The check's outage was notified before the upgrade, and its
    notification row has since been purged. The agent sends that result
    again after the upgrade (its answer was lost): the result is stale, and
    the outage is not notified a second time."""
    ids = await _seed(worker_db)
    down_at = utc_now() - timedelta(minutes=10)
    await _post(ids, down_at, success=False)
    await decide_committed()
    assert await _states(worker_db, ids["alert"]) == ["status:down"]
    async with worker_db() as s:
        await s.execute(
            delete(NotificationLog).where(NotificationLog.alert_id == ids["alert"])
        )
        await s.commit()

    await _upgrade(worker_db)
    async with worker_db() as s:
        cursor = await AlertDecisionCRUD.get_cursor(s, ids["check"])

    await _post(ids, down_at, success=False)
    await decide_committed()

    assert cursor == down_at
    assert await _states(worker_db, ids["alert"]) == []


async def test_a_check_with_no_results_starts_with_no_cursor(
    db: AsyncSession,
) -> None:
    """A check that has stored nothing has nothing decided: its first result
    is decided whatever its timestamp."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    quiet = make_check(agent_id=agent.id)
    db.add(quiet)
    await db.flush()

    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))

    assert await AlertDecisionCRUD.get_cursor(db, quiet.id) is None


async def test_the_pass_lists_each_check_once_the_longest_waiting_first(
    db: AsyncSession,
) -> None:
    """Three checks with queued results, one with many: each is listed
    once, by its oldest queued result, and the limit holds."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(3)]
    db.add_all(checks)
    await db.flush()
    t0 = utc_now() - timedelta(hours=1)
    many, oldest, newest = checks
    keys = [(many.id, t0 + timedelta(seconds=10 + n)) for n in range(500)]
    keys += [(oldest.id, t0), (newest.id, t0 + timedelta(minutes=30))]
    await AlertDecisionCRUD.enqueue(db, keys)

    listed = await AlertDecisionCRUD.checks_with_queued(db, 10)
    assert listed[:3] == [oldest.id, many.id, newest.id]
    assert len(set(listed)) == len(listed)
    assert await AlertDecisionCRUD.checks_with_queued(db, 1) == [oldest.id]


async def test_a_check_whose_alerts_are_all_disabled_has_its_queue_drained(
    worker_db: Any,
) -> None:
    """Results queued while the alert was enabled; the alert is then
    disabled. The next pass drains the check's queue and decides nothing,
    so a check no alert evaluates never holds queued rows."""
    ids = await _seed(worker_db)
    at = utc_now() - timedelta(minutes=1)
    async with worker_db() as s:
        s.add(
            make_check_result(
                check_id=ids["check"],
                agent_id=ids["agent"],
                timestamp=at,
                success=False,
            )
        )
        await s.flush()
        assert (
            await AlertDecisionCoreService.queue_report_results(s, [(ids["check"], at)])
            == 1
        )
        await s.execute(
            update(Alert).where(Alert.id == ids["alert"]).values(is_enabled=False)
        )
        await s.commit()

    await decide_committed()

    async with worker_db() as s:
        queued = await s.scalar(
            select(func.count())
            .select_from(AlertDecisionQueue)
            .where(AlertDecisionQueue.check_id == ids["check"])
        )
        cursor = await s.scalar(
            select(AlertDecisionCursor.decided_through).where(
                AlertDecisionCursor.check_id == ids["check"]
            )
        )
    assert (queued, cursor) == (0, at)
    assert await _states(worker_db, ids["alert"]) == []
