"""A bulk action changes a synthetic check only when its owner is an admin.

The web UI's bulk enable, disable, delete and modify run as maintenance jobs.
Each job acts as the user who started it, judged when it runs: an editor's
job leaves every synthetic check alone and reports it, an admin's changes it.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import (
    bulk_check_delete,
    bulk_check_modify,
    bulk_check_toggle,
)
from app.background.maintenance_worker import JobFailed
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.models.user_model import User
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _setup(db: AsyncSession, role: str) -> dict:
    owner = make_user(role=role)
    home, other = make_agent(), make_agent()
    db.add_all([owner, home, other])
    await db.flush()
    synthetic = make_check(
        agent_id=home.id,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
    )
    ping = make_check(agent_id=home.id)
    db.add_all([synthetic, ping])
    await db.flush()
    # Ids, not rows: a row read after expire_all() would lazy-load outside the loop.
    return {
        "owner": owner.id,
        "other": other.id,
        "synthetic": synthetic.id,
        "ping": ping.id,
    }


def _job(kind: MaintenanceJobKind, owner_id: UUID | None, **params) -> MaintenanceJob:
    return MaintenanceJob(id=uuid4(), kind=kind, params=params, owner_id=owner_id)


def _ids(s: dict) -> list[str]:
    return [str(s["synthetic"]), str(s["ping"])]


async def _fresh(db: AsyncSession, check_id: UUID) -> Check | None:
    await db.flush()  # what the worker's commit writes
    db.expire_all()
    return await db.get(Check, check_id, populate_existing=True)


@pytest.mark.parametrize(("role", "changed"), [("editor", False), ("admin", True)])
async def test_a_bulk_disable_changes_a_synthetic_check_only_for_an_admin(
    db: AsyncSession, role: str, changed: bool
) -> None:
    s = await _setup(db, role)
    job = _job(
        MaintenanceJobKind.BULK_CHECK_TOGGLE,
        s["owner"],
        action="disable",
        check_ids=_ids(s),
    )

    await bulk_check_toggle.handle(db, job)

    assert (await _fresh(db, s["ping"])).enabled is False
    assert (await _fresh(db, s["synthetic"])).enabled is (not changed)


@pytest.mark.parametrize(("role", "changed"), [("editor", False), ("admin", True)])
async def test_a_bulk_delete_removes_a_synthetic_check_only_for_an_admin(
    db: AsyncSession, role: str, changed: bool
) -> None:
    s = await _setup(db, role)
    job = _job(MaintenanceJobKind.BULK_CHECK_DELETE, s["owner"], check_ids=_ids(s))

    await bulk_check_delete.handle(db, job)

    assert await _fresh(db, s["ping"]) is None
    assert (await _fresh(db, s["synthetic"]) is None) is changed


@pytest.mark.parametrize(("role", "changed"), [("editor", False), ("admin", True)])
async def test_a_bulk_move_moves_a_synthetic_check_only_for_an_admin(
    db: AsyncSession, role: str, changed: bool
) -> None:
    s = await _setup(db, role)
    job = _job(
        MaintenanceJobKind.BULK_CHECK_MODIFY,
        s["owner"],
        check_ids=_ids(s),
        update_fields={},
        new_agent_id=str(s["other"]),
        alert_id=None,
    )

    await bulk_check_modify.handle(db, job)

    assert (await _fresh(db, s["ping"])).agent_id == s["other"]
    moved = (await _fresh(db, s["synthetic"])).agent_id == s["other"]
    assert moved is changed


async def test_a_job_with_no_owner_or_a_deactivated_admin_owner_changes_nothing(
    db: AsyncSession,
) -> None:
    s = await _setup(db, "admin")
    owner = await db.get(User, s["owner"])
    owner.is_active = False
    await db.flush()
    for owner_id in (None, s["owner"]):
        job = _job(
            MaintenanceJobKind.BULK_CHECK_TOGGLE,
            owner_id,
            action="disable",
            check_ids=_ids(s),
        )
        with pytest.raises(JobFailed, match="may no longer change checks"):
            await bulk_check_toggle.handle(db, job)
        assert (await _fresh(db, s["synthetic"])).enabled is True
