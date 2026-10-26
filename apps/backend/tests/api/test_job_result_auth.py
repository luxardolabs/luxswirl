"""`POST /api/v1/agent/jobs/{job_id}/results` accepts results only from the job's own agent.

The route used to WRITE the result first and check the caller afterwards, and
only when the job had an agent: a job with no `agent_id` (or whose agent was
gone) took results from anyone with no credential at all. Each refusal below
also asserts the job was left untouched.
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
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_KEY_A = "agent-a-key"
_KEY_B = "agent-b-key"
_RESULT = {"status": "completed", "result": {"hosts": 3}}


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


@pytest_asyncio.fixture
async def world(db: AsyncSession) -> dict[str, object]:
    agent_a = make_agent(api_key_hash=_hash(_KEY_A))
    agent_b = make_agent(api_key_hash=_hash(_KEY_B))
    db.add_all([agent_a, agent_b])
    await db.flush()
    owned = Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=agent_a.id,
        params={"subnet": "10.0.0.0/24"},
        status=JobStatus.RUNNING,
    )
    unassigned = Job(
        job_type=JobType.NETWORK_SCAN,
        agent_id=None,
        params={"subnet": "10.0.1.0/24"},
        status=JobStatus.PENDING,
    )
    db.add_all([owned, unassigned])
    await db.flush()
    return {"owned": owned, "unassigned": unassigned}


async def _post(client: AsyncClient, job: Job, key: str | None) -> int:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = await client.post(
        f"/api/v1/agent/jobs/{job.id}/results", json=_RESULT, headers=headers
    )
    return resp.status_code


async def test_an_unassigned_job_takes_no_results_from_anyone(
    client: AsyncClient, world: dict[str, object], db: AsyncSession
) -> None:
    job = world["unassigned"]
    assert isinstance(job, Job)
    assert await _post(client, job, None) == 403
    assert await _post(client, job, _KEY_A) == 403
    await db.refresh(job)
    assert job.status == JobStatus.PENDING and job.result is None


async def test_no_key_and_a_wrong_key_are_refused_before_any_write(
    client: AsyncClient, world: dict[str, object], db: AsyncSession
) -> None:
    job = world["owned"]
    assert isinstance(job, Job)
    assert await _post(client, job, None) == 401
    assert await _post(client, job, "guess") == 401
    await db.refresh(job)
    assert job.status == JobStatus.RUNNING and job.result is None


async def test_another_agents_key_is_refused(
    client: AsyncClient, world: dict[str, object], db: AsyncSession
) -> None:
    job = world["owned"]
    assert isinstance(job, Job)
    assert await _post(client, job, _KEY_B) == 401
    await db.refresh(job)
    assert job.status == JobStatus.RUNNING


async def test_an_unknown_job_is_not_found(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/agent/jobs/00000000-0000-0000-0000-000000000000/results",
        json=_RESULT,
        headers={"Authorization": f"Bearer {_KEY_A}"},
    )
    assert resp.status_code == 404


async def test_the_assigned_agent_completes_its_job(
    client: AsyncClient, world: dict[str, object], db: AsyncSession
) -> None:
    job = world["owned"]
    assert isinstance(job, Job)
    assert await _post(client, job, _KEY_A) == 200
    await db.refresh(job)
    assert job.status == JobStatus.COMPLETED
