"""Report submission authenticates in ReportingAgentId; exports come from core.

POST /api/v1/reports takes only the agent's own key: the registration key, a
wrong key, or an agent that has no key yet are all refused before anything is
written. The agent check export is built in core and typed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.db.database import get_db
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


def _report(agent_id: str) -> dict[str, object]:
    return {"agent_id": agent_id, "timestamp": utc_now().isoformat(), "checks": []}


async def test_the_agent_key_submits(client: AsyncClient, db: AsyncSession) -> None:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)

    resp = await client.post(
        "/api/v1/reports",
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
        "/api/v1/reports",
        json=_report(str(agent.id)),
        headers={"Authorization": "Bearer anything"},
    )

    assert resp.status_code == 403


async def test_an_unknown_agent_is_not_found(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/reports",
        json=_report(str(uuid4())),
        headers={"Authorization": "Bearer anything"},
    )

    assert resp.status_code == 404


async def test_the_export_lists_the_agents_checks(
    client: AsyncClient, db: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", False)
    agent = make_agent(hostname="edge-host")
    db.add(agent)
    await db.flush()
    db.add_all([make_check(agent_id=agent.id) for _ in range(2)])
    await db.flush()

    resp = await client.get(f"/api/v1/import-export/export/{agent.id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_hostname"] == "edge-host"
    assert body["total_checks"] == 2
    assert len(body["checks"]) == 2


async def test_exporting_an_unknown_agent_is_not_found(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    resp = await client.get(f"/api/v1/import-export/export/{uuid4()}")

    assert resp.status_code == 404


async def test_the_default_config_exports(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    resp = await client.get("/api/v1/import-export/export-agent-config")

    assert resp.status_code == 200, resp.text
    assert resp.json()["total_checks"] == len(resp.json()["checks"])
