"""Every alert write survives its owner's commit (durability locks).

Locks for moving AlertCoreService's session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test is the session owner: it calls
the real service method on a session it commits, then reads back on a SEPARATE
connection, where only committed rows are visible.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.enum_model import NotificationStatus
from app.models.notification_log_model import NotificationLog
from app.schemas.alert_schema import AlertCreate, AlertUpdate
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
from sqlalchemy import func, select

pytestmark = pytest.mark.integration


async def _seed(maker, *rows) -> None:
    async with maker() as s:
        for row in rows:
            s.add(row)
            await s.flush()
        await s.commit()


async def _check_and_provider(maker):
    agent = make_agent()
    check = make_check(agent_id=agent.id)
    provider = make_notification_provider()
    await _seed(maker, agent, check, provider)
    return check.id, provider.id


async def _count(maker, model, **where) -> int:
    async with maker() as other:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return int(await other.scalar(stmt) or 0)


async def test_create_alert_commits_the_alert_and_its_mappings(worker_db) -> None:
    check_id, provider_id = await _check_and_provider(worker_db)

    async with worker_db() as s:
        alert = await AlertCoreService.create_alert(
            s,
            AlertCreate(
                name="db down",
                trigger_type="status_change",
                trigger_config={"on_status": ["error"]},
                notification_provider_ids=[provider_id],
                check_ids=[check_id],
            ),
        )
        alert_id = alert.id
        await s.commit()

    assert await _count(worker_db, Alert, id=alert_id) == 1
    assert await _count(worker_db, AlertNotificationMapping, alert_id=alert_id) == 1
    assert await _count(worker_db, AlertCheckMapping, alert_id=alert_id) == 1


async def test_update_alert_is_committed(worker_db) -> None:
    alert = make_alert(name="before")
    await _seed(worker_db, alert)

    async with worker_db() as s:
        updated = await AlertCoreService.update_alert(
            s, alert.id, AlertUpdate(name="after")
        )
        assert updated.name == "after"
        await s.commit()

    async with worker_db() as other:
        assert (
            await other.scalar(select(Alert.name).where(Alert.id == alert.id))
            == "after"
        )


@pytest.mark.parametrize("hard", [True, False])
async def test_delete_alert_is_committed(worker_db, hard: bool) -> None:
    alert = make_alert()
    await _seed(worker_db, alert)

    async with worker_db() as s:
        await AlertCoreService.delete_alert(s, alert.id, hard_delete=hard)
        await s.commit()

    async with worker_db() as other:
        row = await other.scalar(select(Alert).where(Alert.id == alert.id))
    if hard:
        assert row is None
    else:
        assert row is not None and row.deleted_at is not None


async def test_add_provider_and_check_mappings_are_committed(worker_db) -> None:
    check_id, provider_id = await _check_and_provider(worker_db)
    alert = make_alert()
    await _seed(worker_db, alert)

    async with worker_db() as s:
        provider_mapping_id = await AlertCoreService.add_notification_provider(
            s, alert.id, provider_id
        )
        check_mapping_id = await AlertCoreService.add_check(s, alert.id, check_id)
        assert isinstance(provider_mapping_id, UUID)
        assert isinstance(check_mapping_id, UUID)
        await s.commit()

    assert (
        await _count(worker_db, AlertNotificationMapping, id=provider_mapping_id) == 1
    )
    assert await _count(worker_db, AlertCheckMapping, id=check_mapping_id) == 1


async def test_global_alerts_are_assigned_to_a_new_check(worker_db) -> None:
    check_id, _ = await _check_and_provider(worker_db)
    global_alert = make_alert(is_global=True)
    await _seed(worker_db, global_alert)

    async with worker_db() as s:
        await AlertCoreService.assign_global_alerts_to_check(s, check_id)
        await s.commit()

    assert (
        await _count(
            worker_db, AlertCheckMapping, alert_id=global_alert.id, check_id=check_id
        )
        == 1
    )


async def test_snooze_is_committed(worker_db) -> None:
    check_id, _ = await _check_and_provider(worker_db)
    alert = make_alert()
    await _seed(
        worker_db, alert, make_alert_check_mapping(alert_id=alert.id, check_id=check_id)
    )

    async with worker_db() as s:
        await AlertCoreService.snooze_alert_check(s, alert.id, check_id, minutes=15)
        await s.commit()

    async with worker_db() as other:
        until = await other.scalar(
            select(AlertCheckMapping.snoozed_until).where(
                AlertCheckMapping.alert_id == alert.id,
                AlertCheckMapping.check_id == check_id,
            )
        )
    assert until is not None and until > utc_now() + timedelta(minutes=10)


async def test_parent_down_suppression_log_is_committed(worker_db) -> None:
    agent = make_agent()
    parent = make_check(agent_id=agent.id, display_name="gateway")
    child = make_check(agent_id=agent.id, depends_on_check_id=parent.id)
    provider = make_notification_provider()
    alert = make_alert()
    parent_down = make_check_result(
        check_id=parent.id, agent_id=agent.id, success=False
    )
    child_result = make_check_result(
        check_id=child.id, agent_id=agent.id, success=False
    )
    await _seed(
        worker_db,
        agent,
        parent,
        child,
        provider,
        alert,
        make_alert_notification_mapping(
            alert_id=alert.id, notification_provider_id=provider.id
        ),
        parent_down,
        child_result,
    )

    async with worker_db() as s:
        loaded_child = await s.get(Check, child.id)
        loaded_alert = await s.get(Alert, alert.id)
        loaded_result = await s.scalar(
            select(CheckResult).where(CheckResult.id == child_result.id)
        )
        assert loaded_child and loaded_alert and loaded_result
        suppressed = await AlertEvaluationCoreService._handle_parent_suppression(
            s, loaded_alert, loaded_child, loaded_result
        )
        assert suppressed is True
        await s.commit()

    async with worker_db() as other:
        statuses = list(
            await other.scalars(
                select(NotificationLog.status).where(
                    NotificationLog.alert_id == alert.id
                )
            )
        )
    assert statuses == [NotificationStatus.SUPPRESSED]
