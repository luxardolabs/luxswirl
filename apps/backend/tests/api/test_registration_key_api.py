"""The registration-key API answers the keys core returns as DTOs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
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


async def test_a_key_through_its_lifecycle(client: AsyncClient) -> None:
    base = "/api/v1/registration-keys"
    created = await client.post(base, json={"name": "fleet-a"})
    assert created.status_code == 201, created.text
    key_id = created.json()["id"]
    assert created.json()["key"]

    fetched = await client.get(f"{base}/{key_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "active"
    assert "key" not in fetched.json()

    listed = await client.get(base)
    assert key_id in [k["id"] for k in listed.json()["keys"]]

    updated = await client.patch(f"{base}/{key_id}", json={"description": "edge"})
    assert updated.json()["description"] == "edge"

    revoked = await client.post(f"{base}/{key_id}/revoke", json={"reason": "rotated"})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_reason"] == "rotated"


async def test_an_unknown_key_is_not_found(client: AsyncClient) -> None:
    assert (await client.get(f"/api/v1/registration-keys/{uuid4()}")).status_code == 404
