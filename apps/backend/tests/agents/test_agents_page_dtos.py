"""The agents page renders pending agents and sparklines from core DTOs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.db.database import get_db
from app.main import app
from app.models.agent_metric_model import AgentMetric
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            c.cookies.set(settings.security.session_cookie_name, token)
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_pending_and_active_agents_render(
    client: AsyncClient, db: AsyncSession
) -> None:
    pending = make_agent(hostname="awaiting-approval", approval_status="pending")
    active = make_agent(
        agent_name="edge-1", approval_status="active", last_seen=utc_now()
    )
    db.add_all([pending, active])
    await db.flush()
    db.add_all(
        [
            AgentMetric(
                agent_id=active.id,
                timestamp=utc_now() - timedelta(minutes=minutes),
                cpu_percent=12.5,
                memory_mb=128,
                queue_depth=0,
                checks_executed=10,
            )
            for minutes in (1, 2, 3)
        ]
    )
    await db.flush()

    resp = await client.get("/agents")

    assert resp.status_code == 200, resp.text[:500]
    assert "awaiting-approval" in resp.text
    assert "edge-1" in resp.text
