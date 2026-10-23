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
from fixtures.factories import make_agent, make_check, make_user
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


async def test_a_page_of_several_agents_renders_each_ones_check_counts(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Each card loaded (and decrypted) every one of its agent's checks only to
    count them, one agent at a time; the counts now come from one grouped query
    for the page, and must still land on the right card."""
    agents = [
        make_agent(
            agent_name=f"fleet-{n}", approval_status="active", last_seen=utc_now()
        )
        for n in range(3)
    ]
    db.add_all(agents)
    await db.flush()
    for n, agent in enumerate(agents):
        db.add_all(
            [
                make_check(agent_id=agent.id, display_name=f"c{n}_{i}", enabled=i < n)
                for i in range(n + 2)
            ]
        )
        db.add(
            AgentMetric(
                agent_id=agent.id,
                timestamp=utc_now() - timedelta(minutes=1),
                cpu_percent=5.0,
                memory_mb=64,
                queue_depth=0,
                checks_executed=n,
            )
        )
    await db.flush()

    resp = await client.get("/agents")

    assert resp.status_code == 200, resp.text[:500]
    for n in range(3):
        assert f"fleet-{n}" in resp.text
        assert (
            f'{n}<span class="text-sm text-dark-text-muted">/{n + 2}</span>'
            in resp.text
        )


async def test_an_agent_with_no_heartbeat_stats_yet_renders(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A freshly registered agent has sent no heartbeat, so every stat column is
    NULL. Building its card must not fail on those NULLs (they rendered a 500
    when the response schema required numbers)."""
    stats = (
        "uptime_seconds",
        "checks_total",
        "checks_active",
        "checks_executed_total",
        "checks_succeeded_total",
        "checks_failed_total",
        "cpu_percent",
        "memory_mb",
        "queue_depth",
    )
    fresh = make_agent(agent_name="just-registered", **dict.fromkeys(stats))
    pending = make_agent(
        hostname="fresh-pending", approval_status="pending", **dict.fromkeys(stats)
    )
    db.add_all([fresh, pending])
    await db.flush()

    resp = await client.get("/agents")

    assert resp.status_code == 200, resp.text[:500]
    assert "just-registered" in resp.text
    assert "fresh-pending" in resp.text
