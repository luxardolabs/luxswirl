"""Key recovery is bound to the agent, not just to the shared registration key.

The registration key is shared by every agent install, so it alone must never
mint a key for an agent. Registration hands the new agent a random recovery
secret once, stored only as a hash; recover-key needs the registration key AND
that agent's secret (header `X-Agent-Recovery-Secret`). An unknown agent and a
wrong secret are refused identically. An agent registered before the secret
existed (no hash) cannot recover: it re-registers and is approved again.

Auth is ON in every test here, and every outcome is a SECURITY AUDIT line that
never carries the secret.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from app.models.agent_model import Agent
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient, Response
from limits import parse
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET_HEADER = "X-Agent-Recovery-Secret"
AUDIT_LOGGER = "luxswirl.services.agent"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db
        await db.flush()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        limiter.reset()


@pytest_asyncio.fixture
async def reg_key(db: AsyncSession) -> str:
    _, token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="recovery-tests"), created_by="tests"
    )
    return token


async def _register(client: AsyncClient, reg_key: str) -> tuple[UUID, str]:
    """Register over the API, as the agent does: its id and recovery secret."""
    resp = await client.post(
        "/api/v1/agents/register",
        json={"hostname": "recovery-host"},
        headers={"Authorization": f"Bearer {reg_key}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return UUID(body["agent_id"]), body["recovery_secret"]


async def _approve(db: AsyncSession, agent_id: UUID) -> str:
    _, key = await AgentCoreService.approve_agent(db, agent_id)
    return key


async def _recover(
    client: AsyncClient,
    agent_id: UUID | str,
    *,
    bearer: str | None,
    secret: str | None,
) -> Response:
    headers = {}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if secret is not None:
        headers[SECRET_HEADER] = secret
    return await client.post(f"/api/v1/agents/{agent_id}/recover-key", headers=headers)


async def test_registration_returns_a_secret_stored_only_as_its_hash(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    first_id, first = await _register(client, reg_key)
    _, second = await _register(client, reg_key)

    row = await db.get(Agent, first_id)
    assert row is not None
    assert len(first) >= 40 and first != second
    assert row.recovery_secret_hash == hashlib.sha256(first.encode()).hexdigest()
    assert first not in (row.recovery_secret_hash or "")


async def test_the_right_secret_recovers_a_working_key(
    client: AsyncClient, db: AsyncSession, reg_key: str, caplog
) -> None:
    agent_id, secret = await _register(client, reg_key)
    old_key = await _approve(db, agent_id)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        resp = await _recover(client, agent_id, bearer=reg_key, secret=secret)

    assert resp.status_code == 200, resp.text
    new_key = resp.json()["api_key"]
    assert new_key and new_key != old_key
    assert secret not in resp.text
    report = await client.post(
        "/api/v1/reports",
        json={
            "agent_id": str(agent_id),
            "timestamp": "2026-10-22T10:00:00Z",
            "checks": [],
        },
        headers={"Authorization": f"Bearer {new_key}"},
    )
    assert report.status_code == 202, report.text
    assert "SECURITY AUDIT: Agent key recovered" in caplog.text
    assert secret not in caplog.text


async def test_the_wrong_secret_is_refused(
    client: AsyncClient, db: AsyncSession, reg_key: str, caplog
) -> None:
    agent_id, _ = await _register(client, reg_key)
    key = await _approve(db, agent_id)
    guess = "luxswirl_rs_" + "0" * 64

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        resp = await _recover(client, agent_id, bearer=reg_key, secret=guess)

    assert resp.status_code == 401, resp.text
    assert "SECURITY AUDIT: Agent key recovery refused" in caplog.text
    assert guess not in caplog.text
    # The refused attempt rotated nothing: the agent's key still works.
    still = await client.post(
        "/api/v1/reports",
        json={
            "agent_id": str(agent_id),
            "timestamp": "2026-10-22T10:00:00Z",
            "checks": [],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert still.status_code == 202, still.text


async def test_my_secret_cannot_recover_another_agent(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    """One compromised install holds the registration key and its own secret:
    neither takes over another agent by its id."""
    victim_id, _ = await _register(client, reg_key)
    await _approve(db, victim_id)
    _, my_secret = await _register(client, reg_key)

    resp = await _recover(client, victim_id, bearer=reg_key, secret=my_secret)

    assert resp.status_code == 401, resp.text


async def test_no_secret_is_refused(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    """The registration key alone is what used to be enough: it is not."""
    agent_id, _ = await _register(client, reg_key)
    await _approve(db, agent_id)

    missing = await _recover(client, agent_id, bearer=reg_key, secret=None)
    empty = await _recover(client, agent_id, bearer=reg_key, secret="")

    assert missing.status_code == 401, missing.text
    assert empty.status_code == 401, empty.text


async def test_the_secret_without_the_registration_key_is_refused(
    client: AsyncClient, db: AsyncSession, reg_key: str, caplog
) -> None:
    agent_id, secret = await _register(client, reg_key)
    key = await _approve(db, agent_id)

    with caplog.at_level(logging.INFO, logger="luxswirl.security"):
        no_bearer = await _recover(client, agent_id, bearer=None, secret=secret)
    assert "SECURITY AUDIT: Agent key recovery refused" in caplog.text
    agent_key_bearer = await _recover(client, agent_id, bearer=key, secret=secret)
    secret_as_bearer = await _recover(client, agent_id, bearer=secret, secret=secret)

    assert no_bearer.status_code == 401, no_bearer.text
    assert agent_key_bearer.status_code == 401, agent_key_bearer.text
    assert secret_as_bearer.status_code == 401, secret_as_bearer.text


async def test_an_agent_registered_before_secrets_cannot_recover(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    """No hash on the row: no secret matches, not even an empty one."""
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    await _approve(db, agent.id)
    assert agent.recovery_secret_hash is None

    empty = await _recover(client, agent.id, bearer=reg_key, secret="")
    anything = await _recover(client, agent.id, bearer=reg_key, secret="anything")
    hash_of_empty = await _recover(
        client, agent.id, bearer=reg_key, secret=hashlib.sha256(b"").hexdigest()
    )

    assert empty.status_code == 401, empty.text
    assert anything.status_code == 401, anything.text
    assert hash_of_empty.status_code == 401, hash_of_empty.text


async def test_an_unknown_agent_is_refused_exactly_like_a_wrong_secret(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    """Nothing tells a prober whether an agent id exists."""
    agent_id, _ = await _register(client, reg_key)
    await _approve(db, agent_id)
    guess = "luxswirl_rs_" + "1" * 64

    unknown = await _recover(client, uuid4(), bearer=reg_key, secret=guess)
    wrong = await _recover(client, agent_id, bearer=reg_key, secret=guess)

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_the_right_secret_cannot_skip_approval(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    """A pending agent holds its secret from registration, but gets no key
    until an operator approves it."""
    agent_id, secret = await _register(client, reg_key)

    resp = await _recover(client, agent_id, bearer=reg_key, secret=secret)

    assert resp.status_code == 403, resp.text
    row = await db.get(Agent, agent_id)
    assert row is not None and row.api_key_hash is None


async def test_the_secret_in_the_query_string_is_not_read(
    client: AsyncClient, db: AsyncSession, reg_key: str
) -> None:
    """Only the header carries it, so it never lands in an access log URL."""
    agent_id, secret = await _register(client, reg_key)
    await _approve(db, agent_id)

    resp = await client.post(
        f"/api/v1/agents/{agent_id}/recover-key",
        params={"recovery_secret": secret, SECRET_HEADER: secret},
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 401, resp.text


async def test_secret_guesses_spend_the_recovery_rate_limit(
    client: AsyncClient, db: AsyncSession, reg_key: str, monkeypatch
) -> None:
    """Holding the registration key, a host cannot guess secrets without limit:
    the refusal is raised in the route body, inside the limit."""
    monkeypatch.setattr(settings.security, "rate_limit_enabled", True)
    agent_id, _ = await _register(client, reg_key)
    await _approve(db, agent_id)
    allowed = parse(settings.security.registration_rate_limit).amount

    statuses = [
        (
            await _recover(client, agent_id, bearer=reg_key, secret=f"guess-{n}")
        ).status_code
        for n in range(allowed + 1)
    ]

    assert set(statuses[:allowed]) == {401}, statuses
    assert statuses[allowed] == 429, statuses
