"""The agent lifecycle and job-stats routes answer with their declared models.

They returned bare dicts (`response_model=dict`), so openapi.json promised
nothing about them. The wire shape is unchanged; these pin it, typed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_approve_issues_a_key_and_reports_active(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge-approve", approval_status="pending")
    db.add(agent)
    await db.flush()

    resp = await client.post("/api/v1/agents/edge-approve/approve")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"message", "agent_name", "status", "api_key"}
    assert body["agent_name"] == "edge-approve"
    assert body["status"] == "active"
    assert body["api_key"]


async def test_reject_reports_rejected(client: AsyncClient, db: AsyncSession) -> None:
    db.add(make_agent(agent_name="edge-reject", approval_status="pending"))
    await db.flush()

    resp = await client.post("/api/v1/agents/edge-reject/reject")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "message": "Agent rejected",
        "agent_name": "edge-reject",
        "status": "rejected",
    }


async def test_job_stats_count_by_status_and_total(
    client: AsyncClient, db: AsyncSession
) -> None:
    for status in (
        JobStatus.PENDING,
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.FAILED,
    ):
        db.add(Job(job_type=JobType.NETWORK_SCAN, params={}, status=status))
    await db.flush()

    resp = await client.get("/api/v1/jobs/stats/summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending"] == 2
    assert body["running"] == 1
    assert body["failed"] == 1
    assert body["completed"] == 0
    assert body["total"] == 4
    assert body["active"] == 3
    assert body["terminal"] == 1
