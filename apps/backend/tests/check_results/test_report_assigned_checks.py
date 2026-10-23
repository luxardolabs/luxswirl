"""An agent's report is accepted for exactly the checks it was told to run.

GET /api/v1/checks hands an agent its manual checks plus every replicate and
distribute check whose selector it matches. The report path looked results up
by the check's own `agent_id` column instead, so a replicate or distribute
check run by any other selected agent had every result dropped.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
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


async def test_a_distribute_check_takes_results_only_from_its_assigned_agent(
    db: AsyncSession,
) -> None:
    owner, other, _ = await _agents(db)
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
    unassigned = other if assigned[0] is owner else owner

    taken = await CheckResultCoreService.process_agent_report(
        db, _report(assigned[0].id, check.id)
    )
    refused = await CheckResultCoreService.process_agent_report(
        db, _report(unassigned.id, check.id)
    )

    assert taken["results_processed"] == 1
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
