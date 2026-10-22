"""GET /api/v1/checks: an agent fetches the checks it should run.

The route now authenticates through `AuthenticatedAgentId` and hands core the
agent's id, so no ORM agent crosses the seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def agent_with_key(db: AsyncSession) -> tuple[str, str, set[str]]:
    agent = make_agent(agent_name="edge-1", approval_status="pending")
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(2)]
    db.add_all(checks)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return str(agent.id), key, {str(c.id) for c in checks}


async def test_the_agent_lists_its_checks_with_its_key(
    client: AsyncClient, agent_with_key: tuple[str, str, set[str]]
) -> None:
    agent_id, key, check_ids = agent_with_key

    resp = await client.get(
        "/api/v1/checks",
        params={"agent_id": agent_id},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    assert {c["id"] for c in resp.json()["checks"]} == check_ids


async def test_a_wrong_key_is_refused(
    client: AsyncClient, agent_with_key: tuple[str, str, set[str]]
) -> None:
    agent_id, _, _ = agent_with_key

    resp = await client.get(
        "/api/v1/checks",
        params={"agent_id": agent_id},
        headers={"Authorization": "Bearer not-the-key"},
    )

    assert resp.status_code in (401, 403)


async def test_an_unknown_agent_is_not_found(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/checks",
        params={"agent_id": str(uuid4())},
        headers={"Authorization": "Bearer anything"},
    )

    assert resp.status_code == 404
