"""Persistence-asserting tests for cleanup_core_service (LUXSWIRL-127 / -191).

These cleanups DELETE rows older than a cutoff. Pre-fix (LUXSWIRL-191) the delete
ran but the worker session never committed, so a fresh connection still saw the
"deleted" rows — and a test that only checked the returned count would have passed
anyway. So each test seeds an OLD row and a RECENT row, runs the cleanup, then
reads from a FRESH session and asserts the old one is actually GONE and the recent
one survived. That fresh-session read is the assertion the bug failed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fixtures.factories import make_agent
from sqlalchemy import select

from app.crud.scheduler_crud import JobExecutionCRUD
from app.models.agent_model import Agent
from app.models.enum_model import SchedulerJobCategory
from app.models.scheduler_model import JobExecution
from app.services.core.cleanup_core_service import (
    cleanup_old_job_executions,
    cleanup_stale_agents,
)

pytestmark = pytest.mark.integration

_OLD = datetime(2000, 1, 1, tzinfo=UTC)


class TestCleanupOldJobExecutions:
    async def test_deletes_old_keeps_recent_and_commits(self, worker_db):
        async with worker_db() as s:
            await JobExecutionCRUD.create_execution(
                s,
                job_key="old",
                job_name="Old",
                category=SchedulerJobCategory.MONITORING,
                started_at=_OLD,
                status="success",
            )
            await JobExecutionCRUD.create_execution(
                s,
                job_key="new",
                job_name="New",
                category=SchedulerJobCategory.MONITORING,
                started_at=datetime.now(UTC),
                status="success",
            )
            await s.commit()

        result = await cleanup_old_job_executions(days_to_keep=90)
        assert result["deleted"] == 1

        async with worker_db() as s:
            keys = (await s.execute(select(JobExecution.job_key))).scalars().all()
        assert set(keys) == {"new"}, "old execution was not actually deleted + committed"


class TestCleanupStaleAgents:
    async def test_deletes_stale_keeps_active(self, worker_db):
        async with worker_db() as s:
            s.add(make_agent(agent_name="stale", last_seen=_OLD))
            s.add(make_agent(agent_name="fresh", last_seen=datetime.now(UTC)))
            await s.commit()

        result = await cleanup_stale_agents(inactive_days=30)
        assert result["deleted"] == 1
        assert result["agents"] == ["stale"]

        async with worker_db() as s:
            names = (await s.execute(select(Agent.agent_name))).scalars().all()
        assert set(names) == {"fresh"}, "stale agent was not actually deleted + committed"

    async def test_no_stale_agents_is_a_noop(self, worker_db):
        async with worker_db() as s:
            s.add(make_agent(agent_name="fresh", last_seen=datetime.now(UTC)))
            await s.commit()

        result = await cleanup_stale_agents(inactive_days=30)
        assert result["deleted"] == 0
        assert result["agents"] == []
