"""A check deleted while an agent's report is in flight costs only its own results.

The report's checks are read, then its results inserted. A check deleted in
between (by an editor, a bulk delete, an agent delete) has no row for its results
to reference: those results are counted in `results_failed`, and the rest of the
report is stored.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.schemas.check_result_schema import AgentReportRequest, CheckResultCreate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _result(check_id: UUID) -> CheckResultCreate:
    return CheckResultCreate(
        timestamp=utc_now(),
        success=True,
        latency_ms=5.0,
        check_id=str(check_id),
        display_name="probe",
        check_type="ping",
        target="127.0.0.1",
    )


async def _stored(db: AsyncSession, check_id: UUID) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(CheckResult)
            .where(CheckResult.check_id == check_id)
        )
        or 0
    )


async def test_a_check_deleted_after_the_report_read_it_fails_only_its_results(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = make_agent(agent_name="reporter")
    db.add(agent)
    await db.flush()
    kept = make_check(agent_id=agent.id, display_name="kept")
    gone = make_check(agent_id=agent.id, display_name="gone")
    db.add_all([kept, gone])
    await db.flush()
    gone_id = gone.id

    read = AgentAssignmentCoreService.get_reportable_checks

    async def read_then_delete(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        checks = await read(session, *args, **kwargs)
        # The delete lands after the report read its checks, before the insert.
        await session.execute(delete(Check).where(Check.id == gone_id))
        return checks

    monkeypatch.setattr(
        AgentAssignmentCoreService, "get_reportable_checks", read_then_delete
    )

    result = await CheckResultCoreService.process_agent_report(
        db,
        AgentReportRequest(
            agent_id=agent.id, checks=[_result(kept.id), _result(gone_id)]
        ),
    )

    assert result["results_processed"] == 1
    assert result["results_failed"] == 1
    assert await _stored(db, kept.id) == 1
    assert await _stored(db, gone_id) == 0
