"""`POST /api/v1/jobs/{job_id}/start`: the job's own agent says it began the job.

The job moves from assigned to running with `started_at` stamped, so an
operator can tell a job the agent never picked up from one that started and
has not come back. Only the job's agent may say so, a repeat changes nothing,
and a job already finished or cancelled stays as it is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import bcrypt
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.services.core.job_core_service import JobCoreService
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_KEY_A = "agent-a-key"
_KEY_B = "agent-b-key"


def _hash(key: str) -> str:
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        limiter.reset()


async def _job(db: AsyncSession, status: JobStatus = JobStatus.ASSIGNED) -> Job:
    agent_a = make_agent(api_key_hash=_hash(_KEY_A))
    agent_b = make_agent(api_key_hash=_hash(_KEY_B))
    db.add_all([agent_a, agent_b])
    await db.flush()
    job = Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=agent_a.id,
        params={"subnet": "10.0.0.0/24"},
        status=status,
    )
    db.add(job)
    await db.flush()
    return job


async def _start(client: AsyncClient, job: Job, key: str | None = _KEY_A):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return await client.post(f"/api/v1/jobs/{job.id}/start", headers=headers)


async def test_the_jobs_agent_starting_it_makes_it_running(
    client: AsyncClient, db: AsyncSession
) -> None:
    job = await _job(db)

    resp = await _start(client, job)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert resp.json()["started_at"] is not None
    await db.refresh(job)
    assert (job.status, job.started_at is not None) == (JobStatus.RUNNING, True)


async def test_a_repeated_start_keeps_the_first_start_time(
    client: AsyncClient, db: AsyncSession
) -> None:
    job = await _job(db)
    await _start(client, job)
    await db.refresh(job)
    first = job.started_at

    resp = await _start(client, job)

    assert resp.status_code == 200
    await db.refresh(job)
    assert job.started_at == first


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
)
async def test_a_finished_job_is_not_reopened(
    client: AsyncClient, db: AsyncSession, status: JobStatus
) -> None:
    job = await _job(db, status)

    resp = await _start(client, job)

    assert resp.status_code == 200
    await db.refresh(job)
    assert (job.status, job.started_at) == (status, None)


@pytest.mark.parametrize(("key", "code"), [(_KEY_B, 401), (None, 401)])
async def test_only_the_jobs_own_agent_may_start_it(
    client: AsyncClient, db: AsyncSession, key: str | None, code: int
) -> None:
    job = await _job(db)

    resp = await _start(client, job, key)

    assert resp.status_code in (code, 403), resp.text
    await db.refresh(job)
    assert (job.status, job.started_at) == (JobStatus.ASSIGNED, None)


async def test_an_unknown_job_is_not_found(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/jobs/00000000-0000-0000-0000-000000000000/start",
        headers={"Authorization": f"Bearer {_KEY_A}"},
    )

    assert resp.status_code == 404


async def test_a_running_job_can_still_be_cancelled(
    client: AsyncClient, db: AsyncSession
) -> None:
    job = await _job(db)
    await _start(client, job)

    cancelled = await JobCoreService.cancel_job(db, job.id)

    assert cancelled is not None
    assert cancelled.status == JobStatus.CANCELLED


async def test_a_job_not_yet_dispatched_stays_pending(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Dispatch only sends pending jobs, so one marked running before it was
    dispatched would never reach the agent and look running forever."""
    job = await _job(db, JobStatus.PENDING)

    resp = await _start(client, job)

    assert resp.status_code == 200
    await db.refresh(job)
    assert (job.status, job.started_at) == (JobStatus.PENDING, None)
