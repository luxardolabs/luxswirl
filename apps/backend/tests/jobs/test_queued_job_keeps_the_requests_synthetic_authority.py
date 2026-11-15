"""A queued job acts on a synthetic check only with the authority of its request.

Bulk delete, bulk modify, bulk enable/disable and agent delete run in the
maintenance worker after the request that queued them. Whether the request
could act on a synthetic check (an admin by the web, an admin's token holding
`checks:admin` by the API) is stored on the job, and the job uses it only
while its owner is still an active admin. A check made synthetic, or a
synthetic check added to the agent, between queueing and running is left
alone by a job whose request could not have touched it. A job queued without
the stored judgment leaves every synthetic check alone.

Through the real worker (`process_next_job`, its own committing sessions).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.core.exceptions import DuplicateResourceException
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.models.user_model import User
from app.schemas.admin_check_schema import AdminBulkChecks, AdminBulkModify
from app.schemas.auth_schema import CurrentUserDTO
from app.services.core.admin_agent_core_service import AdminAgentCoreService
from app.services.core.admin_check_core_service import AdminCheckCoreService
from app.services.views.checks_view_service import ChecksViewService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration

SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://a.example')\n"
    "    return {'status': 'success', 'steps': ['Loaded page']}\n"
)
SYNTHETIC = {
    "check_type": "synthetic",
    "target": "https://a.example",
    "script_code": SCRIPT,
}


async def _user(maker, role: str = "admin") -> UUID:
    user = make_user(role=role)
    async with maker() as s:
        s.add(user)
        await s.commit()
        return user.id


async def _authed(maker, user_id: UUID) -> CurrentUserDTO:
    async with maker() as s:
        return CurrentUserDTO.model_validate(await s.get(User, user_id))


async def _agent(maker) -> UUID:
    agent = make_agent()
    async with maker() as s:
        s.add(agent)
        await s.commit()
        return agent.id


async def _check(maker, agent_id: UUID, **kw) -> UUID:
    check = make_check(agent_id=agent_id, **kw)
    async with maker() as s:
        s.add(check)
        await s.commit()
        return check.id


async def _turn_synthetic(maker, check_id: UUID) -> None:
    async with maker() as s:
        check = await s.get(Check, check_id)
        assert check is not None
        for field, value in SYNTHETIC.items():
            setattr(check, field, value)
        await s.commit()


async def _get(maker, check_id: UUID) -> Check | None:
    async with maker() as s:
        return (
            await s.execute(select(Check).where(Check.id == check_id))
        ).scalar_one_or_none()


async def _run() -> None:
    register_all()
    assert await process_next_job()


# ── An admin's token without checks:admin ───────────────────────────────────


async def test_a_bulk_delete_queued_without_checks_admin_spares_a_check_made_synthetic(
    worker_db,
) -> None:
    owner = await _user(worker_db)
    check_id = await _check(worker_db, await _agent(worker_db))
    async with worker_db() as s:
        await AdminCheckCoreService.enqueue_bulk_delete(
            s, owner, AdminBulkChecks(check_ids=[check_id]), actor_is_admin=False
        )
        await s.commit()

    await _turn_synthetic(worker_db, check_id)
    await _run()

    assert await _get(worker_db, check_id) is not None


async def test_a_bulk_move_queued_without_checks_admin_spares_a_check_made_synthetic(
    worker_db,
) -> None:
    owner = await _user(worker_db)
    source, target = await _agent(worker_db), await _agent(worker_db)
    check_id = await _check(worker_db, source)
    async with worker_db() as s:
        await AdminCheckCoreService.enqueue_bulk_modify(
            s,
            owner,
            AdminBulkModify(check_ids=[check_id], agent_id=target),
            actor_is_admin=False,
        )
        await s.commit()

    await _turn_synthetic(worker_db, check_id)
    await _run()

    check = await _get(worker_db, check_id)
    assert check is not None
    assert check.agent_id == source


async def test_an_agent_delete_queued_without_checks_admin_spares_a_synthetic_added_later(
    worker_db,
) -> None:
    owner = await _user(worker_db)
    agent_id = await _agent(worker_db)
    async with worker_db() as s:
        await AdminAgentCoreService.enqueue_delete(
            s, owner, agent_id, actor_is_admin=False
        )
        await s.commit()

    synthetic = await _check(worker_db, agent_id, **SYNTHETIC)
    await _run()

    assert await _get(worker_db, synthetic) is not None


# ── A request that could act on synthetic checks still does ─────────────────


async def test_a_bulk_delete_queued_with_checks_admin_deletes_a_check_made_synthetic(
    worker_db,
) -> None:
    owner = await _user(worker_db)
    check_id = await _check(worker_db, await _agent(worker_db))
    async with worker_db() as s:
        await AdminCheckCoreService.enqueue_bulk_delete(
            s, owner, AdminBulkChecks(check_ids=[check_id]), actor_is_admin=True
        )
        await s.commit()

    await _turn_synthetic(worker_db, check_id)
    await _run()

    assert await _get(worker_db, check_id) is None


@pytest.mark.parametrize(("role", "deleted"), [("admin", True), ("editor", False)])
async def test_a_web_bulk_delete_acts_on_a_synthetic_check_as_its_users_role(
    worker_db, role: str, deleted: bool
) -> None:
    user = await _authed(worker_db, await _user(worker_db, role))
    check_id = await _check(worker_db, await _agent(worker_db), **SYNTHETIC)
    async with worker_db() as s:
        await ChecksViewService.enqueue_bulk_check_action(
            s, "delete", "false", None, "all", "", [check_id], user
        )
        await s.commit()

    await _run()

    assert (await _get(worker_db, check_id) is None) is deleted


# ── A job queued without the stored judgment ────────────────────────────────


async def test_a_job_without_the_stored_judgment_spares_every_synthetic_check(
    worker_db,
) -> None:
    owner = await _user(worker_db)
    synthetic = await _check(worker_db, await _agent(worker_db), **SYNTHETIC)
    async with worker_db() as s:
        s.add(
            MaintenanceJob(
                id=uuid4(),
                kind=MaintenanceJobKind.BULK_CHECK_DELETE,
                target_id=None,
                params={"check_ids": [str(synthetic)]},
                owner_id=owner,
                status=MaintenanceJobStatus.QUEUED,
            )
        )
        await s.commit()

    await _run()

    assert await _get(worker_db, synthetic) is not None


# ── Two different requests for one agent are never one job ──────────────────


async def test_an_agent_delete_with_other_authority_than_the_queued_one_is_refused(
    worker_db,
) -> None:
    owner = await _user(worker_db)
    agent_id = await _agent(worker_db)
    async with worker_db() as s:
        first = await AdminAgentCoreService.enqueue_delete(
            s, owner, agent_id, actor_is_admin=False
        )
        await s.commit()

    async with worker_db() as s:
        again = await AdminAgentCoreService.enqueue_delete(
            s, owner, agent_id, actor_is_admin=False
        )
        assert again.id == first.id
        with pytest.raises(DuplicateResourceException):
            await AdminAgentCoreService.enqueue_delete(
                s, owner, agent_id, actor_is_admin=True
            )
