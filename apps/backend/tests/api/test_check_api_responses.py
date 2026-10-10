"""The single-check JSON routes answer one full CheckResponse, built in core.

- POST .../clone never passed the target agent to core, so every clone raised
  TypeError and answered 500. The path's agent is the clone's target.
- Get/create/update built a thinner CheckResponse by hand than the agent's
  check list did, dropping the check-type fields (http_method, ...).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from fixtures.factories import make_agent, make_check
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


async def test_clone_copies_the_check_to_the_path_agent(
    client: AsyncClient, db: AsyncSession
) -> None:
    source_agent, target_agent = (
        make_agent(agent_name="src"),
        make_agent(agent_name="dst"),
    )
    db.add_all([source_agent, target_agent])
    await db.flush()
    check = make_check(agent_id=source_agent.id)
    db.add(check)
    await db.flush()

    resp = await client.post(
        f"/api/v1/agents/{target_agent.id}/checks/{check.id}/clone",
        json={"display_name": "cloned-copy"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_id"] == str(target_agent.id)
    assert body["fully_qualified_name"] == "dst:cloned-copy"


async def test_create_answers_the_check_type_fields(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge")
    db.add(agent)
    await db.flush()

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/checks",
        json={
            "display_name": "api",
            "check_type": "http",
            "target": "https://example.com/health",
            "interval_seconds": 60,
            "http_method": "POST",
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["http_method"] == "POST"
    assert resp.json()["fully_qualified_name"] == "edge:api"


async def test_get_is_bound_to_the_path_agent(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, other = make_agent(), make_agent()
    db.add_all([owner, other])
    await db.flush()
    check = make_check(agent_id=owner.id)
    db.add(check)
    await db.flush()

    assert (
        await client.get(f"/api/v1/agents/{owner.id}/checks/{check.id}")
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/agents/{other.id}/checks/{check.id}")
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/agents/{owner.id}/checks/{uuid4()}")
    ).status_code == 404
