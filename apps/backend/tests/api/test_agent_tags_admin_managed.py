"""An agent's tags are admin-managed once it is approved.

Tags decide which replicate and distribute checks an agent is handed by
GET /api/v1/agent/checks, and so which credential-bearing targets it sees and which
checks it may report on. An agent declares tags at registration and may update
them by heartbeat while pending, as a suggestion the admin sees when approving.
From approval on, its heartbeat's tags never change its row; only an admin does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.agent_model import Agent
from app.schemas.agent_schema import AgentUpdate
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

DB_TARGET = "postgresql://monitor:s3cretpw@db.internal:5432/app"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def registration_token(db: AsyncSession) -> str:
    _, token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="tag-tests"), created_by="tests"
    )
    return token


async def _approved(db: AsyncSession, tags: list[str]) -> tuple[Agent, str]:
    agent = make_agent(approval_status="pending", tags=tags)
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    return agent, key


async def _db_replicate_check(db: AsyncSession, owner: Agent) -> None:
    check = make_check(
        agent_id=owner.id,
        check_type="postgres",
        target=DB_TARGET,
        assignment_mode="replicate",
        agent_selector={"tags": ["role:db"]},
    )
    db.add(check)
    await db.flush()


async def _checks_text(client: AsyncClient, agent: Agent, key: str) -> str:
    resp = await client.get(
        "/api/v1/agent/checks",
        params={"agent_id": str(agent.id)},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.text


async def _heartbeat(
    client: AsyncClient, agent_id: UUID, key: str, tags: list[str]
) -> None:
    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={
            "agent_id": str(agent_id),
            "timestamp": utc_now().isoformat(),
            "status": "online",
            "tags": tags,
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text


async def test_an_agent_cannot_self_assign_replicate_checks_by_heartbeat_tags(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An approved agent that retags itself by heartbeat is not handed a
    replicate check meant for other agents, nor its credential-bearing target."""
    owner, _ = await _approved(db, ["role:db"])
    rogue, rogue_key = await _approved(db, ["role:web"])
    await _db_replicate_check(db, owner)
    assert "s3cretpw" not in await _checks_text(client, rogue, rogue_key)  # control

    await _heartbeat(client, rogue.id, rogue_key, ["role:db"])

    assert "s3cretpw" not in await _checks_text(client, rogue, rogue_key), (
        "agent self-assigned a db check by retagging"
    )


async def test_an_approved_agents_heartbeat_tags_leave_its_tags_unchanged(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent, key = await _approved(db, ["role:web"])

    await _heartbeat(client, agent.id, key, ["role:db", "region:any"])

    await db.refresh(agent)
    assert agent.tags == ["role:web"]


async def test_an_admins_tag_change_takes_effect_on_the_next_check_list(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, _ = await _approved(db, ["role:db"])
    agent, key = await _approved(db, ["role:web"])
    await _db_replicate_check(db, owner)
    assert "s3cretpw" not in await _checks_text(client, agent, key)

    await AgentCoreService.update_agent(
        db, agent.id, AgentUpdate(tags=["role:db"]), actor_is_admin=True
    )

    assert "s3cretpw" in await _checks_text(client, agent, key)
    # ...and the agent's own heartbeat does not undo it.
    await _heartbeat(client, agent.id, key, ["role:web"])
    await db.refresh(agent)
    assert agent.tags == ["role:db"]


async def test_a_pending_agents_registered_tags_are_kept_as_its_initial_tags(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> None:
    """Registration stores the declared tags; a pending agent's heartbeat may
    still update its suggestion, and approval keeps what is there."""
    resp = await client.post(
        "/api/v1/agent/register",
        json={"hostname": "edge-01", "tags": ["role:web", "region:eu"]},
        headers={"Authorization": f"Bearer {registration_token}"},
    )
    assert resp.status_code == 201, resp.text
    agent_id = UUID(resp.json()["agent_id"])
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    assert agent.tags == ["role:web", "region:eu"]

    await _heartbeat(client, agent_id, registration_token, ["role:web", "region:us"])
    await db.refresh(agent)
    assert agent.tags == ["role:web", "region:us"]

    await AgentCoreService.approve_agent(db, agent_id, actor_is_admin=True)
    await db.refresh(agent)
    assert agent.tags == ["role:web", "region:us"]
