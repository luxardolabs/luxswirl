"""The key-recovery refusal lives in core.

recover-key refuses an agent that is not active (403), once the caller has
proved that agent's recovery secret. It used to be checked in the router
against the ORM row.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", False)
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


async def test_recover_key_refuses_an_inactive_agent(
    client: AsyncClient, db: AsyncSession
) -> None:
    secret = "luxswirl_rs_paused-agent-secret"
    agent = make_agent(
        approval_status="paused",
        recovery_secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
    )
    db.add(agent)
    await db.flush()
    _, token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="recover-tests"), created_by="tests"
    )

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/recover-key",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Recovery-Secret": secret,
        },
    )

    assert resp.status_code == 403, resp.text
