"""Check writes survive their owner's commit.

Durability locks for moving CheckCoreService session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection.
"""

from __future__ import annotations

import pytest
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_dependency_core_service import (
    CheckDependencyCoreService,
)
from fixtures.factories import make_agent, make_check
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _seed(maker, n_checks: int = 0) -> tuple[Agent, list[Check]]:
    agent = make_agent()
    checks = [make_check(agent_id=agent.id) for _ in range(n_checks)]
    async with maker() as s:
        s.add(agent)
        await s.flush()
        s.add_all(checks)
        await s.commit()
    return agent, checks


async def _stored(maker, check_id) -> Check | None:
    async with maker() as other:  # separate connection: only committed rows
        return await other.scalar(select(Check).where(Check.id == check_id))


async def test_a_created_check_is_committed(worker_db) -> None:
    agent, _ = await _seed(worker_db)

    async with worker_db() as s:
        created = await CheckCoreService.create_check(
            s,
            agent.id,
            CheckCreate(
                display_name="durable-create",
                check_type="http",
                target="https://example.test",
                http_method="GET",
                interval_seconds=60,
                timeout_seconds=5,
                retry_interval_seconds=30,
                enabled=True,
            ),
            skip_config_update=True,
        )
        assert created.agent.id == agent.id  # the agent comes back loaded
        await s.commit()

    stored = await _stored(worker_db, created.id)
    assert stored is not None and stored.display_name == "durable-create"


async def test_an_updated_check_is_committed(worker_db) -> None:
    _, (check,) = await _seed(worker_db, 1)

    async with worker_db() as s:
        await CheckCoreService.update_check(
            s, check.id, CheckUpdate(display_name="durable-update")
        )
        await s.commit()

    stored = await _stored(worker_db, check.id)
    assert stored is not None and stored.display_name == "durable-update"


async def test_dependents_are_attached_and_detached_durably(worker_db) -> None:
    _, (parent, a, b) = await _seed(worker_db, 3)

    async with worker_db() as s:
        assert await CheckDependencyCoreService.set_dependents(
            s, parent.id, [a.id, b.id], actor_is_admin=True
        ) == (
            2,
            0,
        )
        await s.commit()
    for child in (a, b):
        stored = await _stored(worker_db, child.id)
        assert stored is not None and stored.depends_on_check_id == parent.id

    async with worker_db() as s:
        assert await CheckDependencyCoreService.set_dependents(
            s, parent.id, [a.id], actor_is_admin=True
        ) == (0, 1)
        await s.commit()
    stored_a = await _stored(worker_db, a.id)
    stored_b = await _stored(worker_db, b.id)
    assert stored_a is not None and stored_a.depends_on_check_id == parent.id
    assert stored_b is not None and stored_b.depends_on_check_id is None


async def test_a_deleted_check_is_gone_after_commit(worker_db) -> None:
    _, (check,) = await _seed(worker_db, 1)

    async with worker_db() as s:
        await CheckCoreService.delete_check(s, check.id)
        await s.commit()

    assert await _stored(worker_db, check.id) is None
