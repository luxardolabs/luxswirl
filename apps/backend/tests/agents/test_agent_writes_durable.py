"""Every agent write survives its owner's commit.

Durability locks for moving AgentCoreService session writes into crud
(`fw.no_session_mutation_outside_crud`). Each test owns the session, calls the
real service method, commits, and reads back on a SEPARATE connection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.models.agent_metric_model import AgentMetric
from app.models.agent_model import Agent
from app.models.enum_model import AgentApprovalStatus, AgentStatus
from app.schemas.agent_schema import AgentCreate, AgentHeartbeat, AgentUpdate
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent
from sqlalchemy import func, select

pytestmark = pytest.mark.integration


async def _stored(maker, agent_id: UUID) -> Agent | None:
    async with maker() as other:  # separate connection: only committed rows
        return await other.scalar(select(Agent).where(Agent.id == agent_id))


async def _seed(maker, **overrides: Any) -> UUID:
    agent = make_agent(**overrides)
    async with maker() as s:
        s.add(agent)
        await s.commit()
    return agent.id


async def test_create_update_and_delete_are_committed(worker_db) -> None:
    async with worker_db() as s:
        created = await AgentCoreService.create_agent(
            s, AgentCreate(agent_name="durable-agent", hostname="durable.test")
        )
        await s.commit()
    stored = await _stored(worker_db, created.id)
    assert stored is not None and stored.agent_name == "durable-agent"

    async with worker_db() as s:
        await AgentCoreService.update_agent(
            s, created.id, AgentUpdate(agent_name="renamed-agent")
        )
        await s.commit()
    stored = await _stored(worker_db, created.id)
    assert stored is not None and stored.agent_name == "renamed-agent"

    async with worker_db() as s:
        await AgentCoreService.delete_agent(s, created.id)
        await s.commit()
    assert await _stored(worker_db, created.id) is None


async def test_a_registered_agent_is_committed_pending(worker_db) -> None:
    async with worker_db() as s:
        registered = await AgentCoreService.register_agent(s, hostname="new.test")
        await s.commit()

    stored = await _stored(worker_db, registered.id)
    assert stored is not None
    assert stored.approval_status == AgentApprovalStatus.PENDING


async def test_last_seen_is_committed(worker_db) -> None:
    agent_id = await _seed(worker_db)

    async with worker_db() as s:
        await AgentCoreService.update_agent_last_seen(s, agent_id, "run-durable")
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None and stored.agent_run_id == "run-durable"


async def test_a_heartbeat_commits_the_agent_and_its_metric(worker_db) -> None:
    agent_id = await _seed(worker_db)

    async with worker_db() as s:
        await AgentCoreService.update_from_heartbeat(
            s,
            agent_id,
            AgentHeartbeat(
                agent_id=agent_id,
                timestamp=utc_now(),
                status=AgentStatus.ONLINE,
                queue_depth=17,
            ),
        )
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None and stored.queue_depth == 17
    async with worker_db() as other:
        metrics = await other.scalar(
            select(func.count())
            .select_from(AgentMetric)
            .where(AgentMetric.agent_id == agent_id)
        )
    assert metrics == 1


async def test_a_regenerated_key_is_committed(worker_db) -> None:
    agent_id = await _seed(worker_db, api_key_hash="old-hash")

    async with worker_db() as s:
        await AgentCoreService.regenerate_agent_key(s, agent_id)
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None and stored.api_key_hash not in (None, "old-hash")


async def test_a_revoked_key_is_committed(worker_db) -> None:
    agent_id = await _seed(worker_db, api_key_hash="old-hash")

    async with worker_db() as s:
        await AgentCoreService.revoke_agent_key(s, agent_id)
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None and stored.api_key_hash is None


async def test_an_approval_is_committed_with_a_key(worker_db) -> None:
    agent_id = await _seed(worker_db, approval_status=AgentApprovalStatus.PENDING)

    async with worker_db() as s:
        await AgentCoreService.approve_agent(s, agent_id)
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None
    assert stored.approval_status == AgentApprovalStatus.ACTIVE
    assert stored.api_key_hash is not None


Transition = Callable[[Any, UUID], Awaitable[object]]

TRANSITIONS: list[tuple[str, AgentApprovalStatus, Transition, AgentApprovalStatus]] = [
    (
        "reject",
        AgentApprovalStatus.PENDING,
        AgentCoreService.reject_agent,
        AgentApprovalStatus.REJECTED,
    ),
    (
        "pause",
        AgentApprovalStatus.ACTIVE,
        AgentCoreService.pause_agent,
        AgentApprovalStatus.PAUSED,
    ),
    (
        "resume",
        AgentApprovalStatus.PAUSED,
        AgentCoreService.resume_agent,
        AgentApprovalStatus.ACTIVE,
    ),
    (
        "disable",
        AgentApprovalStatus.ACTIVE,
        AgentCoreService.disable_agent,
        AgentApprovalStatus.DISABLED,
    ),
    (
        "enable",
        AgentApprovalStatus.DISABLED,
        AgentCoreService.enable_agent,
        AgentApprovalStatus.ACTIVE,
    ),
]


@pytest.mark.parametrize(
    ("before", "transition", "after"),
    [t[1:] for t in TRANSITIONS],
    ids=[t[0] for t in TRANSITIONS],
)
async def test_a_status_transition_is_committed(
    worker_db,
    before: AgentApprovalStatus,
    transition: Transition,
    after: AgentApprovalStatus,
) -> None:
    agent_id = await _seed(worker_db, approval_status=before)

    async with worker_db() as s:
        await transition(s, agent_id)
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None and stored.approval_status == after


async def test_a_forced_reload_is_committed(worker_db) -> None:
    agent_id = await _seed(worker_db, checks_updated_at=None)

    async with worker_db() as s:
        await AgentCoreService.force_reload(s, agent_id)
        await s.commit()

    stored = await _stored(worker_db, agent_id)
    assert stored is not None and stored.checks_updated_at is not None
