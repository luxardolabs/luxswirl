"""Registration-key management on the JSON API is admin-only.

A registration key lets a new agent join, so minting, editing, revoking or
deleting one is an admin act. On the JSON API the admin credential is an API
bearer token (`require_api_admin`); without one, nothing happens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_TOKEN = "admin-api-token"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    monkeypatch.setattr(settings.security, "auth_tokens", [_TOKEN])

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


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_creating_a_key_without_the_admin_token_is_refused(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/registration-keys", json={"name": "x"}, headers=headers
    )
    assert resp.status_code == 401
    listing = await client.get(
        "/api/v1/registration-keys", headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    assert listing.json()["total"] == 0


async def test_the_admin_token_creates_a_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/registration-keys",
        json={"name": "edge-site"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "edge-site"
    assert resp.json()["key"]
