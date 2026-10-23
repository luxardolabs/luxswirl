"""An agent's report is accepted for the checks it may run.

Its manual checks, every replicate check whose selector it matches, and every
distribute check whose pool it is in. A result for any other check is refused.

Only active agents are in a distribute check's pool: an agent that may not run
checks (pending, paused, disabled, rejected) never wins one.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _report(agent_id: UUID, check_id: UUID) -> AgentReportRequest:
    return AgentReportRequest(
        agent_id=agent_id,
        checks=[
            CheckResultCreate(
                timestamp=utc_now(),
                success=True,
                latency_ms=5.0,
                check_id=str(check_id),
                display_name="probe",
                check_type="ping",
                target="127.0.0.1",
            )
        ],
    )


async def _agents(db: AsyncSession):
    owner = make_agent(agent_name="owner", tags=["role:probe"])
    other = make_agent(agent_name="other", tags=["role:probe"])
    outsider = make_agent(agent_name="outsider", tags=["role:db"])
    db.add_all([owner, other, outsider])
    await db.flush()
    return owner, other, outsider


async def test_a_replicate_check_takes_results_from_every_selected_agent(
    db: AsyncSession,
) -> None:
    owner, other, _ = await _agents(db)
    check = make_check(
        agent_id=owner.id,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:probe"]},
    )
    db.add(check)
    await db.flush()

    for agent in (owner, other):
        result = await CheckResultCoreService.process_agent_report(
            db, _report(agent.id, check.id)
        )
        assert result["results_processed"] == 1, agent.agent_name


async def test_an_agent_the_selector_does_not_match_is_refused(
    db: AsyncSession,
) -> None:
    owner, _, outsider = await _agents(db)
    check = make_check(
        agent_id=owner.id,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:probe"]},
    )
    db.add(check)
    await db.flush()

    result = await CheckResultCoreService.process_agent_report(
        db, _report(outsider.id, check.id)
    )

    assert result["results_processed"] == 0
    assert result["results_failed"] == 1


async def test_a_distribute_check_runs_on_one_agent_and_takes_results_from_its_pool(
    db: AsyncSession,
) -> None:
    """One agent of the pool is handed the check, but any agent of the pool may
    report it: the check moves between them as they go offline and come back,
    and results measured before a move arrive after it."""
    owner, other, outsider = await _agents(db)
    check = make_check(
        agent_id=owner.id,
        assignment_mode="distribute",
        agent_selector={"tags": ["role:probe"]},
    )
    db.add(check)
    await db.flush()
    assigned = [
        a
        for a in (owner, other)
        if check.id
        in {c.id for c in await AgentAssignmentCoreService.get_checks_for_agent(db, a)}
    ]
    assert len(assigned) == 1

    from_pool = [
        await CheckResultCoreService.process_agent_report(db, _report(a.id, check.id))
        for a in (owner, other)
    ]
    refused = await CheckResultCoreService.process_agent_report(
        db, _report(outsider.id, check.id)
    )

    assert [r["results_processed"] for r in from_pool] == [1, 1]
    assert refused["results_processed"] == 0


async def test_another_agents_manual_check_is_refused(db: AsyncSession) -> None:
    owner, other, _ = await _agents(db)
    check = make_check(agent_id=owner.id)
    db.add(check)
    await db.flush()

    result = await CheckResultCoreService.process_agent_report(
        db, _report(other.id, check.id)
    )

    assert result["results_processed"] == 0


async def test_a_pending_registration_cannot_steal_a_distribute_check(
    db: AsyncSession,
) -> None:
    """Anyone with the shared registration key can register agents carrying any
    tags. Pending agents are not in the distribute pool, so they cannot win the
    hash and take the check away from the only approved agent."""
    owner = make_agent(agent_name="owner", tags=["role:probe"])
    db.add(owner)
    await db.flush()
    check = make_check(
        agent_id=owner.id,
        assignment_mode="distribute",
        agent_selector={"tags": ["role:probe"]},
    )
    db.add(check)
    await db.flush()

    taken = await CheckResultCoreService.process_agent_report(
        db, _report(owner.id, check.id)
    )
    assert taken["results_processed"] == 1  # control

    for _ in range(40):
        await AgentCoreService.register_agent(
            db, hostname="squatter", tags=["role:probe"]
        )
    await db.flush()
    owner_checks = await AgentAssignmentCoreService.get_checks_for_agent(db, owner)
    still_taken = await CheckResultCoreService.process_agent_report(
        db, _report(owner.id, check.id)
    )

    assert check.id in {c.id for c in owner_checks}
    assert still_taken["results_processed"] == 1


@pytest.mark.parametrize("status", ["pending", "paused", "disabled", "rejected"])
async def test_only_active_agents_are_in_the_distribute_pool(
    db: AsyncSession, status: str
) -> None:
    """An agent that may not run checks never holds a distribute check: every
    one of them goes to an active agent."""
    active = make_agent(agent_name="active", tags=["role:probe"])
    idle = [
        make_agent(agent_name=f"idle-{i}", tags=["role:probe"], approval_status=status)
        for i in range(20)
    ]
    db.add_all([active, *idle])
    await db.flush()
    checks = [
        make_check(
            agent_id=active.id,
            assignment_mode="distribute",
            agent_selector={"tags": ["role:probe"]},
        )
        for _ in range(5)
    ]
    db.add_all(checks)
    await db.flush()

    active_checks = await AgentAssignmentCoreService.get_checks_for_agent(db, active)

    assert {c.id for c in checks} <= {c.id for c in active_checks}
