"""Which checks an agent is handed, and the dependency pickers.

Both read the check table on every call — the assignment one on every agent's
check sync — so they are pinned here by behaviour, independent of how the rows
are fetched: manual checks only to their owner, replicate to every matching
agent, distribute to exactly one matching agent.
"""

from __future__ import annotations

import pytest
from app.core.exceptions import ValidationException
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _names(db: AsyncSession, agent) -> set[str]:
    checks = await AgentAssignmentCoreService.get_checks_for_agent(db, agent)
    return {c.display_name for c in checks}


async def test_each_agent_gets_exactly_its_share(db: AsyncSession) -> None:
    east = make_agent(agent_name="east", tags=["region:east", "role:probe"])
    west = make_agent(agent_name="west", tags=["region:west", "role:probe"])
    db.add_all([east, west])
    await db.flush()
    db.add_all(
        [
            make_check(agent_id=east.id, display_name="east_manual"),
            make_check(agent_id=west.id, display_name="west_manual"),
            make_check(
                agent_id=east.id,
                display_name="all_probes",
                assignment_mode="replicate",
                agent_selector={"tags": ["role:probe"]},
            ),
            make_check(
                agent_id=east.id,
                display_name="west_only",
                assignment_mode="replicate",
                agent_selector={"tags": ["region:west"]},
            ),
            make_check(
                agent_id=east.id,
                display_name="by_name",
                assignment_mode="replicate",
                agent_selector={"agent_ids": ["east"]},
            ),
            make_check(
                agent_id=east.id,
                display_name="one_probe",
                assignment_mode="distribute",
                agent_selector={"tags": ["role:probe"]},
            ),
        ]
    )
    await db.flush()

    east_names = await _names(db, east)
    west_names = await _names(db, west)

    assert east_names - {"one_probe"} == {"east_manual", "all_probes", "by_name"}
    assert west_names - {"one_probe"} == {"west_manual", "all_probes", "west_only"}
    # Distributed to exactly one of the two matching agents.
    assert ("one_probe" in east_names) != ("one_probe" in west_names)


async def test_an_agent_matching_no_selector_gets_only_its_manual_checks(
    db: AsyncSession,
) -> None:
    owner = make_agent(agent_name="owner")
    loner = make_agent(agent_name="loner", tags=[])
    db.add_all([owner, loner])
    await db.flush()
    db.add_all(
        [
            make_check(agent_id=loner.id, display_name="mine"),
            make_check(
                agent_id=owner.id,
                display_name="tagged",
                assignment_mode="replicate",
                agent_selector={"tags": ["role:probe"]},
            ),
            make_check(
                agent_id=owner.id,
                display_name="no_selector",
                assignment_mode="distribute",
                agent_selector=None,
            ),
        ]
    )
    await db.flush()

    assert await _names(db, loner) == {"mine"}


async def test_dependency_pickers(db: AsyncSession) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    parent = make_check(agent_id=agent.id, display_name="parent")
    other_parent = make_check(agent_id=agent.id, display_name="other_parent")
    free = make_check(agent_id=agent.id, display_name="free")
    db.add_all([parent, other_parent, free])
    await db.flush()
    child = make_check(
        agent_id=agent.id, display_name="child", depends_on_check_id=other_parent.id
    )
    db.add(child)
    await db.flush()

    eligible = {
        c.display_name
        for c in await CheckCoreService.list_eligible_parents(db, free.id)
    }
    assert eligible == {"parent", "other_parent"}

    potential = {
        c.display_name
        for c in await CheckCoreService.list_potential_dependents(db, parent.id)
    }
    # Not itself, and not a check that is already someone's parent.
    assert potential == {"free", "child"}


async def test_set_dependents_adds_removes_and_refuses_a_parent(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    parent = make_check(agent_id=agent.id, display_name="p")
    a = make_check(agent_id=agent.id, display_name="a")
    b = make_check(agent_id=agent.id, display_name="b")
    db.add_all([parent, a, b])
    await db.flush()
    b.depends_on_check_id = parent.id
    await db.flush()

    added, removed = await CheckCoreService.set_dependents(db, parent.id, [a.id])

    assert (added, removed) == (1, 1)
    assert a.depends_on_check_id == parent.id
    assert b.depends_on_check_id is None

    grandchild = make_check(
        agent_id=agent.id, display_name="g", depends_on_check_id=a.id
    )
    fresh = make_check(agent_id=agent.id, display_name="fresh")
    db.add_all([grandchild, fresh])
    await db.flush()
    # `a` is now a parent itself, so it cannot become anyone's child.
    with pytest.raises(ValidationException, match="itself a parent"):
        await CheckCoreService.set_dependents(db, fresh.id, [a.id])
