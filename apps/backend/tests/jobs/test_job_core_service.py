"""Unit + integration tests for JobCoreService.

Focused coverage on the LUXSWIRL-188 rework (LUXSWIRL-127): the runner-sentinel
resolution (uuid | "server" | none) and the job lifecycle (create / status
transitions / result submission / cancel guard).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fixtures.factories import make_agent
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AgentNotFoundException
from app.schemas.job_schema import JobCreate, JobResultSubmit
from app.services.core.job_core_service import JobCoreService

svc = JobCoreService


# ---------------------------------------------------------------------------
# resolve_runner_filter — pure (the 188 sentinel logic)
# ---------------------------------------------------------------------------


class TestResolveRunnerFilter:
    def test_none_token_means_no_filter(self):
        assert svc.resolve_runner_filter(None) == (None, False)

    def test_empty_token_means_no_filter(self):
        assert svc.resolve_runner_filter("") == (None, False)

    def test_server_token_means_server_only(self):
        # the canonical "server" sentinel → (agent_id=None, server_only=True)
        assert svc.resolve_runner_filter("server") == (None, True)

    def test_uuid_token_means_agent_filter(self):
        u = uuid4()
        assert svc.resolve_runner_filter(str(u)) == (u, False)

    def test_invalid_token_raises(self):
        # a non-uuid, non-"server" token is a client error (propagates → 400)
        with pytest.raises(ValueError):
            svc.resolve_runner_filter("not-a-uuid")


# ---------------------------------------------------------------------------
# Job lifecycle — DB
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestJobLifecycle:
    @staticmethod
    def _data(**over):
        d = {"job_type": "network_scan", "params": {}}
        d.update(over)
        return JobCreate(**d)

    async def test_create_server_job_is_pending(self, db: AsyncSession):
        job = await svc.create_job(db, self._data())
        assert job.status == "pending"
        assert job.agent_id is None  # NULL agent_id = server runner
        assert job.expires_at is not None

    async def test_create_job_with_valid_agent(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        job = await svc.create_job(db, self._data(agent_id=agent.id))
        assert job.agent_id == agent.id

    async def test_create_job_with_unknown_agent_raises(self, db: AsyncSession):
        with pytest.raises(AgentNotFoundException):
            await svc.create_job(db, self._data(agent_id=uuid4()))

    async def test_status_running_sets_started_at(self, db: AsyncSession):
        job = await svc.create_job(db, self._data())
        updated = await svc.update_job_status(db, job.id, "running")
        assert updated.status == "running"
        assert updated.started_at is not None

    async def test_status_completed_sets_completed_at(self, db: AsyncSession):
        job = await svc.create_job(db, self._data())
        updated = await svc.update_job_status(db, job.id, "completed")
        assert updated.completed_at is not None

    async def test_status_update_on_unknown_job_returns_none(self, db: AsyncSession):
        assert await svc.update_job_status(db, uuid4(), "running") is None

    async def test_submit_completed_stores_result_data(self, db: AsyncSession):
        job = await svc.create_job(db, self._data())
        out = await svc.submit_job_result(
            db,
            job.id,
            JobResultSubmit(status="completed", result={"discovered_hosts": [{"ip": "10.0.0.7"}]}),
        )
        assert out.status == "completed"
        assert out.completed_at is not None
        # the PAYLOAD is actually persisted, not just the status flipped
        assert out.result is not None
        assert out.result.get("discovered_hosts") == [{"ip": "10.0.0.7"}]

    async def test_submit_failed_result(self, db: AsyncSession):
        job = await svc.create_job(db, self._data())
        out = await svc.submit_job_result(
            db, job.id, JobResultSubmit(status="failed", error="boom")
        )
        assert out.status == "failed"

    async def test_submit_result_unknown_job_returns_none(self, db: AsyncSession):
        out = await svc.submit_job_result(
            db, uuid4(), JobResultSubmit(status="completed", result={})
        )
        assert out is None

    async def test_cancel_pending_job(self, db: AsyncSession):
        job = await svc.create_job(db, self._data())
        out = await svc.cancel_job(db, job.id)
        assert out is not None
        assert out.status == "cancelled"

    async def test_cannot_cancel_running_job(self, db: AsyncSession):
        # guard: only pending/assigned jobs can be cancelled
        job = await svc.create_job(db, self._data())
        await svc.update_job_status(db, job.id, "running")
        assert await svc.cancel_job(db, job.id) is None

    async def test_cancel_unknown_job_returns_none(self, db: AsyncSession):
        assert await svc.cancel_job(db, uuid4()) is None

    async def test_late_result_does_not_resurrect_cancelled_job(self, db: AsyncSession):
        # ADVERSARIAL: a job is cancelled, then a (stale) result arrives from the
        # agent that had already picked it up. It must NOT flip back to completed.
        job = await svc.create_job(db, self._data())
        await svc.cancel_job(db, job.id)
        out = await svc.submit_job_result(
            db, job.id, JobResultSubmit(status="completed", result={})
        )
        assert out.status == "cancelled"


@pytest.mark.integration
class TestDispatch:
    """The agent-polling hot path: get_jobs_for_dispatch — what every agent hits
    every heartbeat to pick up work."""

    @staticmethod
    async def _agent(db):
        a = make_agent()
        db.add(a)
        await db.flush()
        return a

    @staticmethod
    def _data(agent_id, job_type="network_scan"):
        return JobCreate(job_type=job_type, params={"subnet": "10.0.0.0/24"}, agent_id=agent_id)

    async def test_agent_gets_its_pending_jobs_in_dispatch_shape(self, db: AsyncSession):
        agent = await self._agent(db)
        await svc.create_job(db, self._data(agent.id, "network_scan"))
        await svc.create_job(db, self._data(agent.id, "network_discover"))

        dispatched = await svc.get_jobs_for_dispatch(db, agent.id)
        assert len(dispatched) == 2
        assert set(dispatched[0]) >= {
            "job_id",
            "job_type",
            "params",
            "priority",
            "timeout_seconds",
        }
        # execution timeout is derived from job_type (not the ping timeout in params)
        by_type = {j["job_type"]: j["timeout_seconds"] for j in dispatched}
        assert by_type["network_scan"] == 600
        assert by_type["network_discover"] == 60

    async def test_dispatch_is_per_agent_and_marks_assigned(self, db: AsyncSession):
        a1 = await self._agent(db)
        a2 = await self._agent(db)
        await svc.create_job(db, self._data(a1.id))
        await svc.create_job(db, self._data(a2.id))

        # a1 polls → gets ONLY its own job
        first = await svc.get_jobs_for_dispatch(db, a1.id)
        assert len(first) == 1
        # side effect: dispatched jobs are marked assigned → a re-poll yields nothing
        # (this is what prevents the same job being handed out twice)
        assert await svc.get_jobs_for_dispatch(db, a1.id) == []
        # a2's job is untouched
        assert len(await svc.get_jobs_for_dispatch(db, a2.id)) == 1
