"""JobCRUD's job listing (keyword-only `skip`/`limit`), status summary and purge."""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.datetime_utils import utc_now
from app.crud.job_crud import JobCRUD
from app.models.enum_model import JobType
from app.models.job_model import Job
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _job(status: str, priority: int = 50) -> Job:
    return Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=None,
        params={},
        status=status,
        priority=priority,
    )


async def test_status_summary_counts_each_status(db: AsyncSession) -> None:
    db.add_all([_job("pending"), _job("pending"), _job("completed"), _job("failed")])
    await db.flush()

    summary = await JobCRUD.get_job_status_summary(db)

    assert summary["total"] == 4
    assert summary["pending"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["running"] == 0


async def test_listing_pages_by_keyword_skip_and_limit(db: AsyncSession) -> None:
    db.add_all([_job("pending") for _ in range(5)])
    await db.flush()

    first, total = await JobCRUD.list_jobs_with_agents(db, skip=0, limit=2)
    rest, _ = await JobCRUD.list_jobs_with_agents(db, skip=2, limit=10)

    assert total == 5
    assert len(first) == 2
    assert len(rest) == 3
    assert {job.id for job, _ in first}.isdisjoint({job.id for job, _ in rest})
    assert all(agent is None for _, agent in first + rest)


async def test_purge_deletes_only_expired_terminal_jobs(db: AsyncSession) -> None:
    past, future = utc_now() - timedelta(hours=1), utc_now() + timedelta(hours=1)
    expired_done = [_job("completed"), _job("failed"), _job("cancelled")]
    for job in expired_done:
        job.expires_at = past
    still_running = _job("running")
    still_running.expires_at = past
    not_yet = _job("completed")
    not_yet.expires_at = future
    no_expiry = _job("completed")
    db.add_all([*expired_done, still_running, not_yet, no_expiry])
    await db.flush()
    kept = {still_running.id, not_yet.id, no_expiry.id}
    db.expunge_all()

    assert await JobCRUD.delete_expired_terminal(db) == 3
    assert await JobCRUD.delete_expired_terminal(db) == 0
    remaining, _ = await JobCRUD.list_jobs_with_agents(db, skip=0, limit=50)
    assert {job.id for job, _ in remaining} == kept
