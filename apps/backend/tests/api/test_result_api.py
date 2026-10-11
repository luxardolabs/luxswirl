"""The results JSON API: latest results per agent, and a check's history.

History is bound to the agent in its path (it used to answer any check's
history under any agent's URL).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.database import get_db
from app.main import app
from fixtures.factories import make_agent, make_check, make_check_result
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


@pytest_asyncio.fixture
async def seeded(db: AsyncSession) -> dict[str, object]:
    owner, other = make_agent(agent_name="owner"), make_agent(agent_name="other")
    db.add_all([owner, other])
    await db.flush()
    check = make_check(agent_id=owner.id, display_name="api")
    db.add(check)
    await db.flush()
    db.add_all(
        [
            make_check_result(
                check_id=check.id,
                agent_id=owner.id,
                success=success,
                timestamp=utc_now() - timedelta(minutes=minutes),
            )
            for minutes, success in ((1, True), (2, False), (3, True))
        ]
    )
    await db.flush()
    return {"check_id": str(check.id)}


async def test_latest_results_for_an_agent(
    client: AsyncClient, seeded: dict[str, object]
) -> None:
    resp = await client.get("/api/v1/agents/owner/results", params={"minutes": 10})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["check_name"] == "api"


async def test_history_is_bound_to_the_path_agent(
    client: AsyncClient, seeded: dict[str, object]
) -> None:
    check_id = seeded["check_id"]

    owned = await client.get(f"/api/v1/agents/owner/checks/{check_id}/history")
    foreign = await client.get(f"/api/v1/agents/other/checks/{check_id}/history")

    assert owned.status_code == 200, owned.text
    assert owned.json()["check_name"] == "api"
    assert len(owned.json()["data_points"]) == 3
    assert foreign.status_code == 404


async def test_an_unknown_agent_has_no_results(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/agents/nobody/results")

    assert resp.status_code == 404
