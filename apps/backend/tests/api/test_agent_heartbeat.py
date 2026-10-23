"""POST /api/v1/heartbeat authenticates through AuthenticatedHeartbeat.

The agent's own key dispatches jobs. The registration key is accepted for an
agent that has not stored its key yet, but dispatches none. Anything else is
refused before the heartbeat is applied.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from app.models.agent_model import Agent
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent
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


@pytest_asyncio.fixture
async def registration_token(db: AsyncSession) -> str:
    _, token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="heartbeat-tests"), created_by="tests"
    )
    return token


@pytest_asyncio.fixture
async def approved(db: AsyncSession) -> tuple[str, str]:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return str(agent.id), key


def _beat(agent_id: str) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "timestamp": utc_now().isoformat(),
        "status": "online",
    }


async def test_the_agent_key_is_accepted(
    client: AsyncClient, approved: tuple[str, str]
) -> None:
    agent_id, key = approved

    resp = await client.post(
        "/api/v1/heartbeat",
        json=_beat(agent_id),
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["approval_status"] == "active"
    assert resp.json()["message"] is None


async def test_the_registration_key_is_accepted_but_told_to_recover(
    client: AsyncClient, approved: tuple[str, str], registration_token: str
) -> None:
    agent_id, _ = approved

    resp = await client.post(
        "/api/v1/heartbeat",
        json=_beat(agent_id),
        headers={"Authorization": f"Bearer {registration_token}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["jobs"] == []
    assert "recover-key" in resp.json()["message"]


async def test_a_wrong_key_is_refused(
    client: AsyncClient, approved: tuple[str, str]
) -> None:
    agent_id, _ = approved

    resp = await client.post(
        "/api/v1/heartbeat",
        json=_beat(agent_id),
        headers={"Authorization": "Bearer nope"},
    )

    assert resp.status_code in (401, 403)


async def test_an_unknown_agent_is_not_found(
    client: AsyncClient, registration_token: str
) -> None:
    resp = await client.post(
        "/api/v1/heartbeat",
        json=_beat(str(uuid4())),
        headers={"Authorization": f"Bearer {registration_token}"},
    )

    assert resp.status_code == 404


async def test_dead_letter_count_is_stored_on_the_agent(
    client: AsyncClient, approved: tuple[str, str], db: AsyncSession
) -> None:
    """The agent reports how many batches the server refused for good; the
    field must land on the agent row, not be dropped as an unknown key."""
    agent_id, key = approved

    resp = await client.post(
        "/api/v1/heartbeat",
        json={**_beat(agent_id), "dead_letter_count": 4},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    agent = await db.get(Agent, UUID(agent_id))
    assert agent is not None
    await db.refresh(agent)
    assert agent.dead_letter_count == 4


async def test_the_run_id_is_stored_from_the_heartbeat(
    client: AsyncClient, approved: tuple[str, str], db: AsyncSession
) -> None:
    """The heartbeat owns the agent's liveness fields, the run id included:
    the report path no longer writes the agent row."""
    agent_id, key = approved
    run_id = "6f1c2a0e-1111-4222-8333-444455556666"

    resp = await client.post(
        "/api/v1/heartbeat",
        json={**_beat(agent_id), "agent_run_id": run_id},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    agent = await db.get(Agent, UUID(agent_id))
    assert agent is not None
    await db.refresh(agent)
    assert agent.agent_run_id == run_id
