"""The destructive and bulk maintenance-job handlers do what the job asked, once.

The web UI enqueues agent deletes, status-page deletes, bulk check creates and
bulk check modifies as maintenance jobs; the worker runs these handlers. A
delete of a row that is already gone is the outcome the operator asked for, so
it completes instead of failing the job. A job missing its target is refused
before anything is touched.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import (
    agent_delete,
    bulk_check_create,
    bulk_check_modify,
    status_page_delete,
)
from app.models.agent_model import Agent
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.models.status_page_model import StatusPage
from fixtures.factories import make_agent, make_alert, make_check, make_user
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _job(
    db: AsyncSession,
    kind: MaintenanceJobKind,
    *,
    target_id: UUID | None = None,
    params: dict[str, object] | None = None,
) -> MaintenanceJob:
    owner = make_user(role="editor")
    db.add(owner)
    await db.flush()
    job = MaintenanceJob(
        kind=kind, target_id=target_id, params=params or {}, owner_id=owner.id
    )
    db.add(job)
    await db.flush()
    return job


async def _agent_with_checks(
    db: AsyncSession, count: int = 2
) -> tuple[Agent, list[Check]]:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(count)]
    db.add_all(checks)
    await db.flush()
    return agent, checks


async def _count(db: AsyncSession, model: type, **where: object) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    return int(await db.scalar(stmt) or 0)


# --- agent_delete --------------------------------------------------------------


async def test_agent_delete_removes_the_agent_and_its_checks(db: AsyncSession) -> None:
    agent, _ = await _agent_with_checks(db)
    other, _ = await _agent_with_checks(db, count=1)
    agent_id, other_id = agent.id, other.id
    job = await _job(db, MaintenanceJobKind.AGENT_DELETE, target_id=agent_id)

    await agent_delete.handle(db, job)
    db.expire_all()

    assert await _count(db, Agent, id=agent_id) == 0
    assert await _count(db, Check, agent_id=agent_id) == 0
    assert await _count(db, Check, agent_id=other_id) == 1


async def test_agent_delete_of_a_gone_agent_completes(db: AsyncSession) -> None:
    job = await _job(db, MaintenanceJobKind.AGENT_DELETE, target_id=uuid4())

    await agent_delete.handle(db, job)


async def test_agent_delete_without_a_target_is_refused(db: AsyncSession) -> None:
    job = await _job(db, MaintenanceJobKind.AGENT_DELETE)

    with pytest.raises(ValueError, match="target_id"):
        await agent_delete.handle(db, job)


# --- status_page_delete --------------------------------------------------------


async def test_status_page_delete_removes_only_that_page(db: AsyncSession) -> None:
    doomed = StatusPage(name="Doomed", slug=f"doomed-{uuid4().hex[:6]}", items=[])
    kept = StatusPage(name="Kept", slug=f"kept-{uuid4().hex[:6]}", items=[])
    db.add_all([doomed, kept])
    await db.flush()
    doomed_id, kept_id = doomed.id, kept.id
    job = await _job(db, MaintenanceJobKind.STATUS_PAGE_DELETE, target_id=doomed_id)

    await status_page_delete.handle(db, job)
    db.expire_all()

    assert await _count(db, StatusPage, id=doomed_id) == 0
    assert await _count(db, StatusPage, id=kept_id) == 1


async def test_status_page_delete_of_a_gone_page_completes(db: AsyncSession) -> None:
    job = await _job(db, MaintenanceJobKind.STATUS_PAGE_DELETE, target_id=uuid4())

    await status_page_delete.handle(db, job)


async def test_status_page_delete_without_a_target_is_refused(db: AsyncSession) -> None:
    job = await _job(db, MaintenanceJobKind.STATUS_PAGE_DELETE)

    with pytest.raises(ValueError, match="target_id"):
        await status_page_delete.handle(db, job)


# --- bulk_check_create ---------------------------------------------------------


async def test_bulk_create_makes_the_checks_and_wires_the_alert(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    alert = make_alert()
    db.add_all([agent, alert])
    await db.flush()
    job = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        params={
            "agent_id": str(agent.id),
            "requests": [
                {"url": "https://one.example.test"},
                {"url": "https://two.example.test", "display_name": "second"},
            ],
            "alert_ids": [str(alert.id), ""],
        },
    )

    await bulk_check_create.handle(db, job)

    checks = list(await db.scalars(select(Check).where(Check.agent_id == agent.id)))
    assert len(checks) == 2
    assert "second" in {c.display_name for c in checks}
    assert await _count(db, AlertCheckMapping, alert_id=alert.id) == 2


async def test_bulk_create_with_no_requests_creates_nothing(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    job = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        params={"agent_id": str(agent.id), "requests": []},
    )

    await bulk_check_create.handle(db, job)

    assert await _count(db, Check, agent_id=agent.id) == 0


async def test_bulk_create_without_an_agent_is_refused(db: AsyncSession) -> None:
    job = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_CREATE,
        params={"requests": [{"url": "https://one.example.test"}]},
    )

    with pytest.raises(ValueError, match="agent_id"):
        await bulk_check_create.handle(db, job)


# --- bulk_check_modify ---------------------------------------------------------


async def test_bulk_modify_updates_only_the_listed_checks(db: AsyncSession) -> None:
    _, checks = await _agent_with_checks(db, count=3)
    alert = make_alert()
    db.add(alert)
    await db.flush()
    all_ids = [c.id for c in checks]
    chosen, untouched = all_ids[:2], all_ids[2]
    alert_id = alert.id
    job = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_MODIFY,
        params={
            "check_ids": [str(c) for c in chosen],
            "update_fields": {"interval_seconds": 321},
            "alert_id": str(alert_id),
        },
    )

    await bulk_check_modify.handle(db, job)
    db.expire_all()

    intervals = {
        c.id: c.interval_seconds
        for c in await db.scalars(select(Check).where(Check.id.in_(all_ids)))
    }
    assert intervals[chosen[0]] == intervals[chosen[1]] == 321
    assert intervals[untouched] != 321
    assert await _count(db, AlertCheckMapping, alert_id=alert_id) == 2


async def test_bulk_modify_can_clear_every_alert(db: AsyncSession) -> None:
    _, checks = await _agent_with_checks(db, count=2)
    alert = make_alert()
    db.add(alert)
    await db.flush()
    ids = [str(c.id) for c in checks]
    assign = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_MODIFY,
        params={"check_ids": ids, "alert_id": str(alert.id)},
    )
    await bulk_check_modify.handle(db, assign)
    assert await _count(db, AlertCheckMapping, alert_id=alert.id) == 2

    clear = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_MODIFY,
        params={"check_ids": ids, "alert_id": "__clear__"},
    )
    await bulk_check_modify.handle(db, clear)

    assert await _count(db, AlertCheckMapping, alert_id=alert.id) == 0


async def test_bulk_modify_with_no_checks_changes_nothing(db: AsyncSession) -> None:
    _, checks = await _agent_with_checks(db, count=1)
    check_id, before = checks[0].id, checks[0].interval_seconds
    job = await _job(
        db,
        MaintenanceJobKind.BULK_CHECK_MODIFY,
        params={"check_ids": [], "update_fields": {"interval_seconds": 999}},
    )

    await bulk_check_modify.handle(db, job)
    db.expire_all()

    stored = await db.scalar(select(Check.interval_seconds).where(Check.id == check_id))
    assert stored == before
