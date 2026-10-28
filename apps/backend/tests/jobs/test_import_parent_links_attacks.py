"""An import's parents, linked by either import path, against entries that
never named one, chains, and an order the end state does not depend on."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.models.check_model import Check
from app.models.enum_model import (
    ImportMode,
    MaintenanceJobKind,
    MaintenanceJobStatus,
)
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.import_export_schema import CheckExport
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _owner(maker) -> UUID:
    user = make_user(role="admin")
    async with maker() as s:
        s.add(user)
        await s.commit()
        return user.id


async def _agent(maker) -> UUID:
    agent = make_agent()
    async with maker() as s:
        s.add(agent)
        await s.commit()
        return agent.id


async def _checks(maker, agent_id: UUID) -> dict[str, Check]:
    async with maker() as s:
        rows = await s.scalars(select(Check).where(Check.agent_id == agent_id))
        return {c.display_name: c for c in rows}


async def _row(maker, job_id: UUID) -> MaintenanceJob:
    async with maker() as s:
        return (
            await s.execute(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
        ).scalar_one()


async def _import_raw(maker, dst: UUID, checks: list[dict], mode: str) -> UUID:
    """The web upload's path: the entries as the file had them."""
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
        target_id=None,
        params={"agent_id": str(dst), "mode": mode, "checks": checks},
        owner_id=await _owner(maker),
        status=MaintenanceJobStatus.QUEUED,
    )
    async with maker() as s:
        s.add(job)
        await s.commit()
    register_all()
    assert await process_next_job()
    return job.id


async def _import_api(maker, dst: UUID, checks: list[dict], mode: str) -> UUID:
    """The admin API's path: entries held to CheckExport, then enqueued."""
    owner = await _owner(maker)
    async with maker() as s:
        job = await CheckTransferCoreService.enqueue_import_checks(
            s,
            owner,
            dst,
            ImportMode(mode),
            [CheckExport.model_validate(c) for c in checks],
        )
        await s.commit()
    register_all()
    assert await process_next_job()
    return job.id


async def _with_parent(maker) -> tuple[UUID, UUID]:
    """An agent with `child` depending on `parent`; (agent, parent id)."""
    dst = await _agent(maker)
    parent = make_check(agent_id=dst, display_name="parent")
    async with maker() as s:
        s.add(parent)
        await s.flush()
        s.add(
            make_check(
                agent_id=dst, display_name="child", depends_on_check_id=parent.id
            )
        )
        await s.commit()
    return dst, parent.id


# An entry from an export made before depends_on was in it.
OLD_ENTRY = {"name": "child", "check_type": "ping", "target": "127.0.0.2"}


async def test_control_web_replace_with_an_older_entry_keeps_the_parent(
    worker_db,
) -> None:
    dst, parent_id = await _with_parent(worker_db)
    job_id = await _import_raw(worker_db, dst, [OLD_ENTRY], "replace")
    checks = await _checks(worker_db, dst)
    assert checks["child"].target == "127.0.0.2", (
        await _row(worker_db, job_id)
    ).progress
    assert checks["child"].depends_on_check_id == parent_id


async def test_api_replace_with_an_older_entry_keeps_the_parent(worker_db) -> None:
    """The admin API dumps every entry with depends_on: null, so an entry that
    never named a parent clears the one the check has."""
    dst, parent_id = await _with_parent(worker_db)
    job_id = await _import_api(worker_db, dst, [OLD_ENTRY], "replace")
    checks = await _checks(worker_db, dst)
    assert checks["child"].target == "127.0.0.2", (
        await _row(worker_db, job_id)
    ).progress
    assert checks["child"].depends_on_check_id == parent_id


async def test_import_never_makes_a_two_level_chain(worker_db) -> None:
    """Dependencies are single-level: a parent has no parent. An import whose
    entries chain a -> b -> c must not store both links."""
    dst = await _agent(worker_db)
    entries = [
        {"name": "a", "check_type": "ping", "target": "127.0.0.1", "depends_on": "b"},
        {"name": "b", "check_type": "ping", "target": "127.0.0.1", "depends_on": "c"},
        {"name": "c", "check_type": "ping", "target": "127.0.0.1"},
    ]
    job_id = await _import_raw(worker_db, dst, entries, "merge")
    checks = await _checks(worker_db, dst)
    a, b = checks["a"], checks["b"]
    two_levels = a.depends_on_check_id == b.id and b.depends_on_check_id is not None
    assert not two_levels, (await _row(worker_db, job_id)).progress


async def test_control_import_links_a_valid_single_level_set(worker_db) -> None:
    dst = await _agent(worker_db)
    entries = [
        {"name": "a", "check_type": "ping", "target": "127.0.0.1", "depends_on": "c"},
        {"name": "b", "check_type": "ping", "target": "127.0.0.1", "depends_on": "c"},
        {"name": "c", "check_type": "ping", "target": "127.0.0.1"},
    ]
    job_id = await _import_raw(worker_db, dst, entries, "merge")
    checks = await _checks(worker_db, dst)
    progress = (await _row(worker_db, job_id)).progress
    assert checks["a"].depends_on_check_id == checks["c"].id, progress
    assert checks["b"].depends_on_check_id == checks["c"].id, progress


async def test_replace_relink_does_not_depend_on_entry_order(worker_db) -> None:
    """The agent has b -> p. The file says b has no parent and a depends on b:
    a valid single-level end state. Linking in file order refuses a -> b
    because b still has its old parent when a is linked."""
    dst = await _agent(worker_db)
    p = make_check(agent_id=dst, display_name="p")
    async with worker_db() as s:
        s.add(p)
        await s.flush()
        s.add(make_check(agent_id=dst, display_name="b", depends_on_check_id=p.id))
        await s.commit()
    entries = [
        {"name": "a", "check_type": "ping", "target": "127.0.0.1", "depends_on": "b"},
        {"name": "b", "check_type": "ping", "target": "127.0.0.1", "depends_on": None},
        {"name": "p", "check_type": "ping", "target": "127.0.0.1", "depends_on": None},
    ]
    job_id = await _import_raw(worker_db, dst, entries, "replace")
    checks = await _checks(worker_db, dst)
    progress = (await _row(worker_db, job_id)).progress
    assert checks["b"].depends_on_check_id is None, progress
    assert checks["a"].depends_on_check_id == checks["b"].id, progress


async def test_a_failed_update_does_not_break_another_entry_s_link(
    worker_db,
) -> None:
    """Entry `p` (an existing check) fails in the database; entry `c` names
    `p` as its parent. `p` still exists, so `c` should be linked to it."""
    dst = await _agent(worker_db)
    async with worker_db() as s:
        s.add(make_check(agent_id=dst, display_name="p"))
        await s.commit()
    entries = [
        # description is String(1000): the update fails at flush, after the
        # check was changed in the session.
        {
            "name": "p",
            "check_type": "ping",
            "target": "127.0.0.1",
            "description": "x" * 2000,
        },
        {"name": "c", "check_type": "ping", "target": "127.0.0.1", "depends_on": "p"},
    ]
    job_id = await _import_raw(worker_db, dst, entries, "replace")
    checks = await _checks(worker_db, dst)
    progress = (await _row(worker_db, job_id)).progress
    assert checks["c"].depends_on_check_id == checks["p"].id, json.dumps(progress)


async def test_control_link_without_a_failed_update(worker_db) -> None:
    dst = await _agent(worker_db)
    async with worker_db() as s:
        s.add(make_check(agent_id=dst, display_name="p"))
        await s.commit()
    entries = [
        {"name": "p", "check_type": "ping", "target": "127.0.0.1", "description": "ok"},
        {"name": "c", "check_type": "ping", "target": "127.0.0.1", "depends_on": "p"},
    ]
    job_id = await _import_raw(worker_db, dst, entries, "replace")
    checks = await _checks(worker_db, dst)
    progress = (await _row(worker_db, job_id)).progress
    assert checks["c"].depends_on_check_id == checks["p"].id, json.dumps(progress)
