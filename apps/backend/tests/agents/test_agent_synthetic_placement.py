"""An agent change that moves or removes a synthetic check is an admin's.

A synthetic check runs arbitrary code. Renaming or retagging an agent can put
it into (or take it out of) a synthetic check's selector, approving one
starts every synthetic check it already matches, and deleting one removes the
synthetic checks it owns. An editor may still make every other agent change.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.background.maintenance_handlers import agent_delete
from app.core.exceptions import AuthorizationException
from app.models.agent_model import Agent
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.agent_schema import AgentUpdate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent, make_check, make_user
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SELECTOR = {"tags": ["role:browser"]}


async def _replicated_synthetic(db: AsyncSession) -> tuple[Agent, Agent]:
    """A synthetic check replicated to agents tagged role:browser, and an untagged agent."""
    owner = make_agent(tags=["role:browser"])
    other = make_agent(tags=["role:db"])
    db.add_all([owner, other])
    await db.flush()
    db.add(
        make_check(
            agent_id=owner.id,
            check_type="synthetic",
            target="https://a.example",
            script_code="await page.goto(target)",
            assignment_mode="replicate",
            agent_selector=SELECTOR,
        )
    )
    await db.flush()
    return owner, other


async def _runs_synthetic(db: AsyncSession, agent: Agent) -> bool:
    return bool(await AgentAssignmentCoreService.synthetic_checks_reaching(db, agent))


@pytest.mark.parametrize(
    "change",
    [AgentUpdate(tags=["role:browser"]), AgentUpdate(tags=["role:browser", "x"])],
)
async def test_an_editor_cannot_retag_an_agent_into_a_synthetic_checks_reach(
    db: AsyncSession, change: AgentUpdate
) -> None:
    _, other = await _replicated_synthetic(db)
    with pytest.raises(AuthorizationException):
        await AgentCoreService.update_agent(db, other.id, change, actor_is_admin=False)


async def test_an_editor_cannot_retag_an_agent_out_of_a_synthetic_checks_reach(
    db: AsyncSession,
) -> None:
    owner, _ = await _replicated_synthetic(db)
    with pytest.raises(AuthorizationException):
        await AgentCoreService.update_agent(
            db, owner.id, AgentUpdate(tags=["role:db"]), actor_is_admin=False
        )


async def test_an_admin_retags_an_agent_into_a_synthetic_checks_reach(
    db: AsyncSession,
) -> None:
    _, other = await _replicated_synthetic(db)
    await AgentCoreService.update_agent(
        db, other.id, AgentUpdate(tags=["role:browser"]), actor_is_admin=True
    )
    assert await _runs_synthetic(db, other)


async def test_an_editor_still_makes_a_change_that_moves_no_synthetic_check(
    db: AsyncSession,
) -> None:
    owner, other = await _replicated_synthetic(db)
    await AgentCoreService.update_agent(
        db, other.id, AgentUpdate(tags=["role:db", "zone:a"]), actor_is_admin=False
    )
    await AgentCoreService.update_agent(
        db, owner.id, AgentUpdate(tags=["role:browser", "zone:b"]), actor_is_admin=False
    )
    await AgentCoreService.update_agent(
        db, owner.id, AgentUpdate(hostname="new-host"), actor_is_admin=False
    )


async def test_an_editor_cannot_rename_an_agent_into_a_synthetic_checks_reach(
    db: AsyncSession,
) -> None:
    named = make_agent(agent_name="old-name")
    db.add(named)
    await db.flush()
    db.add(
        make_check(
            agent_id=named.id,
            check_type="synthetic",
            target="https://a.example",
            script_code="await page.goto(target)",
            assignment_mode="replicate",
            agent_selector={"agent_ids": ["browser-1"]},
        )
    )
    await db.flush()
    with pytest.raises(AuthorizationException):
        await AgentCoreService.update_agent(
            db, named.id, AgentUpdate(agent_name="browser-1"), actor_is_admin=False
        )


@pytest.mark.parametrize("actor_is_admin", [False, True])
async def test_approving_an_agent_a_synthetic_check_reaches_is_an_admins(
    db: AsyncSession, actor_is_admin: bool
) -> None:
    await _replicated_synthetic(db)
    pending = make_agent(approval_status="pending", tags=["role:browser"])
    db.add(pending)
    await db.flush()
    if actor_is_admin:
        await AgentCoreService.approve_agent(db, pending.id, actor_is_admin=True)
    else:
        with pytest.raises(AuthorizationException):
            await AgentCoreService.approve_agent(db, pending.id, actor_is_admin=False)


async def test_an_editor_still_approves_an_agent_no_synthetic_check_reaches(
    db: AsyncSession,
) -> None:
    await _replicated_synthetic(db)
    pending = make_agent(approval_status="pending", tags=["role:db"])
    db.add(pending)
    await db.flush()
    await AgentCoreService.approve_agent(db, pending.id, actor_is_admin=False)


@pytest.mark.parametrize(("role", "deleted"), [("editor", False), ("admin", True)])
async def test_deleting_an_agent_that_owns_a_synthetic_check_is_an_admins(
    db: AsyncSession, role: str, deleted: bool
) -> None:
    owner, _ = await _replicated_synthetic(db)
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    owner_id, user_id = owner.id, user.id
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.AGENT_DELETE,
        target_id=owner_id,
        params={},
        owner_id=user_id,
    )

    if deleted:
        await agent_delete.handle(db, job)
    else:
        with pytest.raises(AuthorizationException):
            await agent_delete.handle(db, job)
    await db.flush()
    db.expire_all()

    assert (await db.get(Agent, owner_id) is None) is deleted


async def test_an_editor_still_deletes_an_agent_with_no_synthetic_check(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    db.add(make_check(agent_id=agent.id))
    await db.flush()
    agent_id = agent.id

    await AgentCoreService.delete_agent(db, agent_id, actor_is_admin=False)
    await db.flush()
    db.expire_all()

    assert await db.get(Agent, agent_id) is None
