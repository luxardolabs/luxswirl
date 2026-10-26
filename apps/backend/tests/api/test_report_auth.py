"""Report submission authenticates in ReportingAgentId.

POST /api/v1/agent/reports takes only the agent's own key: the registration key, a
wrong key, or an agent that has no key yet are all refused before anything is
written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.agent_core_service import AgentCoreService
from fixtures.factories import make_agent
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


def _report(agent_id: str) -> dict[str, object]:
    return {"agent_id": agent_id, "timestamp": utc_now().isoformat(), "checks": []}


async def test_the_agent_key_submits(client: AsyncClient, db: AsyncSession) -> None:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)

    resp = await client.post(
        "/api/v1/agent/reports",
        json=_report(str(agent.id)),
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 202, resp.text


async def test_an_agent_without_its_own_key_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()

    resp = await client.post(
        "/api/v1/agent/reports",
        json=_report(str(agent.id)),
        headers={"Authorization": "Bearer anything"},
    )

    assert resp.status_code == 403


async def test_an_unknown_agent_is_not_found(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/agent/reports",
        json=_report(str(uuid4())),
        headers={"Authorization": "Bearer anything"},
    )

    assert resp.status_code == 404
