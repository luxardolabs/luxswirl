"""The jobs view's cancel/delete/filter paths work from core DTOs."""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.core.exceptions import NotFoundException
from app.models.enum_model import JobType
from app.models.job_model import Job
from app.schemas.agent_schema import AgentOption
from app.services.core.job_core_service import JobCoreService
from app.services.views.jobs_view_service import JobsViewService
from fixtures.factories import make_agent
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_cancel_once_then_refuse(db: AsyncSession) -> None:
    job = Job(job_type=JobType.NETWORK_SCAN, agent_id=None, params={}, status="pending")
    db.add(job)
    await db.flush()

    await JobsViewService.cancel_job(db, job.id)

    with pytest.raises(NotFoundException):
        await JobsViewService.cancel_job(db, job.id)


async def test_deleting_an_unknown_job_is_not_found(db: AsyncSession) -> None:
    with pytest.raises(NotFoundException):
        await JobsViewService.handle_delete_job(db, uuid4())


async def test_the_agent_filter_lists_options(db: AsyncSession) -> None:
    agent = make_agent(agent_name="runner", approval_status="active")
    db.add(agent)
    await db.flush()
    db.add(
        Job(
            job_type=JobType.NETWORK_SCAN,
            agent_id=agent.id,
            params={},
            status="pending",
        )
    )
    await db.flush()

    options = await JobCoreService.get_agents_with_jobs(db)

    assert [(o.id, o.agent_name) for o in options] == [(agent.id, "runner")]
    assert all(isinstance(o, AgentOption) for o in options)
