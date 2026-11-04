"""POST /api/v1/agent/heartbeat authenticates through AuthenticatedHeartbeat.

The agent's own key applies the heartbeat and dispatches jobs. The registration
key, shared by every install, applies a heartbeat only for an agent that has no
key of its own yet (a pending agent). For an agent that has one, it only reads
the agent's status back (so an approved agent learns to recover its key):
nothing is written onto the row and no job is dispatched. Anything else is
refused before the heartbeat is applied. last_seen is the server's clock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
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

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


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
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
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
        "/api/v1/agent/heartbeat",
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
        "/api/v1/agent/heartbeat",
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
        "/api/v1/agent/heartbeat",
        json=_beat(agent_id),
        headers={"Authorization": "Bearer nope"},
    )

    assert resp.status_code in (401, 403)


async def test_an_unknown_agent_is_not_found(
    client: AsyncClient, registration_token: str
) -> None:
    resp = await client.post(
        "/api/v1/agent/heartbeat",
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
        "/api/v1/agent/heartbeat",
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
    the report path never writes the agent row."""
    agent_id, key = approved
    run_id = "6f1c2a0e-1111-4222-8333-444455556666"

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={**_beat(agent_id), "agent_run_id": run_id},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    agent = await db.get(Agent, UUID(agent_id))
    assert agent is not None
    await db.refresh(agent)
    assert agent.agent_run_id == run_id


async def test_registration_key_cannot_rewrite_an_approved_agents_row(
    client: AsyncClient,
    approved: tuple[str, str],
    registration_token: str,
    db: AsyncSession,
) -> None:
    """Every install holds the shared registration key. Heartbeating as an
    agent that already has its own key with it only reads that agent's status
    (so an approved agent can learn to recover its key): it writes nothing onto
    the row, neither identity (tags, hostname) nor liveness nor health."""
    agent_id, _ = approved
    agent = await db.get(Agent, UUID(agent_id))
    assert agent is not None
    agent.tags = ["role:probe"]
    await db.flush()
    before = (
        list(agent.tags),
        agent.hostname,
        agent.last_seen,
        agent.agent_run_id,
        agent.dead_letter_count,
    )

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={
            **_beat(agent_id),
            "timestamp": (utc_now() + timedelta(days=365)).isoformat(),
            "tags": ["role:hijacked"],
            "hostname": "hijacker",
            "agent_run_id": "6f1c2a0e-1111-4222-8333-444455556666",
            "dead_letter_count": 99,
        },
        headers={"Authorization": f"Bearer {registration_token}"},
    )

    await db.refresh(agent)
    after = (
        list(agent.tags),
        agent.hostname,
        agent.last_seen,
        agent.agent_run_id,
        agent.dead_letter_count,
    )
    assert after == before, f"registration-key heartbeat rewrote the row: {after}"
    assert resp.status_code == 200, resp.text
    assert resp.json()["jobs"] == []


async def test_registration_key_still_drives_an_agent_without_a_key(
    client: AsyncClient, registration_token: str, db: AsyncSession
) -> None:
    """A pending agent has no key of its own yet: the registration key is its
    only credential, and its heartbeat is applied."""
    agent = make_agent(approval_status="pending", hostname="before")
    db.add(agent)
    await db.flush()

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={**_beat(str(agent.id)), "hostname": "after"},
        headers={"Authorization": f"Bearer {registration_token}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["approval_status"] == "pending"
    await db.refresh(agent)
    assert agent.hostname == "after"


async def test_heartbeat_timestamp_cannot_pin_last_seen_in_the_future(
    client: AsyncClient, approved: tuple[str, str], db: AsyncSession
) -> None:
    """last_seen is the server's clock at receipt: a skewed or hostile agent
    cannot keep itself 'online' after it dies by sending a future timestamp."""
    agent_id, key = approved

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={
            **_beat(agent_id),
            "timestamp": (utc_now() + timedelta(days=365)).isoformat(),
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    agent = await db.get(Agent, UUID(agent_id))
    assert agent is not None
    await db.refresh(agent)
    assert agent.last_seen is not None
    assert agent.last_seen <= utc_now() + timedelta(minutes=5), agent.last_seen


async def _registered_and_approved(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> tuple[UUID, str, str]:
    """An agent registered for real (so it holds a recovery secret), then
    approved: its id, its recovery secret and its own key."""
    resp = await client.post(
        "/api/v1/agent/register",
        json={"hostname": "key-holder"},
        headers={"Authorization": f"Bearer {registration_token}"},
    )
    assert resp.status_code == 201, resp.text
    agent_id = UUID(resp.json()["agent_id"])
    _, key = await AgentCoreService.approve_agent(db, agent_id, actor_is_admin=True)
    return agent_id, resp.json()["recovery_secret"], key


async def test_the_response_says_the_agent_key_was_accepted(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> None:
    agent_id, secret, key = await _registered_and_approved(
        client, db, registration_token
    )

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(str(agent_id)),
        headers={"Authorization": f"Bearer {key}", "X-Agent-Recovery-Secret": secret},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_key_accepted"] is True


async def test_a_regenerated_key_with_the_secret_is_told_its_key_was_not_accepted(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> None:
    """A regenerate keeps the recovery secret: the agent, its key refused,
    proves the secret beside it, and the heartbeat is answered, active, saying
    the key was refused, so the agent recovers its new key."""
    agent_id, secret, old_key = await _registered_and_approved(
        client, db, registration_token
    )
    await AgentCoreService.regenerate_agent_key(db, agent_id)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(str(agent_id)),
        headers={
            "Authorization": f"Bearer {old_key}",
            "X-Agent-Recovery-Secret": secret,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["approval_status"] == "active"
    assert resp.json()["agent_key_accepted"] is False
    assert resp.json()["jobs"] == []


async def test_a_revoke_voids_the_recovery_secret(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> None:
    """A revoke ends the install: the agent's key and its secret are both
    refused at the heartbeat, with the server's own JSON 401."""
    agent_id, secret, old_key = await _registered_and_approved(
        client, db, registration_token
    )
    await AgentCoreService.revoke_agent_key(db, agent_id)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(str(agent_id)),
        headers={
            "Authorization": f"Bearer {old_key}",
            "X-Agent-Recovery-Secret": secret,
        },
    )

    assert resp.status_code == 401, resp.text
    assert "detail" in resp.json() or "error" in resp.json()
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    await db.refresh(agent)
    assert agent.api_key_hash is None
    assert agent.recovery_secret_hash is None


# G5: one captured heartbeat (key and secret) plus the shared registration key
# outlived a Revoke: recover-key issued its holder a new key. A revoke voids
# the secret, so recover-key refuses it.
async def test_g5_after_a_revoke_a_captured_secret_recovers_no_key(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> None:
    agent_id, secret, _ = await _registered_and_approved(client, db, registration_token)
    limiter.reset()  # registration spent one of the address's budget
    await AgentCoreService.revoke_agent_key(db, agent_id)

    stolen = await client.post(
        f"/api/v1/agent/{agent_id}/recover-key",
        headers={
            "Authorization": f"Bearer {registration_token}",
            "X-Agent-Recovery-Secret": secret,
        },
    )

    assert stolen.status_code == 401, stolen.text
    assert "api_key" not in stolen.json()
    agent = await db.get(Agent, agent_id)
    assert agent is not None
    await db.refresh(agent)
    assert agent.api_key_hash is None


# A regenerate keeps the secret: the agent recovers its new key by itself.
async def test_after_a_regenerate_the_secret_still_recovers_a_key(
    client: AsyncClient, db: AsyncSession, registration_token: str
) -> None:
    agent_id, secret, _ = await _registered_and_approved(client, db, registration_token)
    limiter.reset()
    await AgentCoreService.regenerate_agent_key(db, agent_id)

    resp = await client.post(
        f"/api/v1/agent/{agent_id}/recover-key",
        headers={
            "Authorization": f"Bearer {registration_token}",
            "X-Agent-Recovery-Secret": secret,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key"].startswith("luxswirl_ak_")
