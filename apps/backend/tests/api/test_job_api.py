"""The jobs JSON API answers JobResponses built in core, hostnames included."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
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


async def test_create_get_list_and_cancel(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(hostname="scanner-01", approval_status="active")
    db.add(agent)
    await db.flush()

    created = await client.post(
        "/api/v1/jobs",
        json={
            "job_type": "network_scan",
            "agent_id": str(agent.id),
            "params": {"subnet": "10.0.0.0/24"},
        },
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["agent_hostname"] == "scanner-01"

    fetched = await client.get(f"/api/v1/jobs/{job['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["agent_hostname"] == "scanner-01"

    listed = await client.get("/api/v1/jobs", params={"agent_id": str(agent.id)})
    assert listed.status_code == 200
    assert [j["agent_hostname"] for j in listed.json()["jobs"]] == ["scanner-01"]
    assert listed.json()["pending_count"] == 1

    assert (await client.delete(f"/api/v1/jobs/{job['id']}")).status_code == 204


async def test_unknown_jobs_are_not_found(client: AsyncClient) -> None:
    assert (await client.get(f"/api/v1/jobs/{uuid4()}")).status_code == 404
    assert (await client.delete(f"/api/v1/jobs/{uuid4()}")).status_code == 404
