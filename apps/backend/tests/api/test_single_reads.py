"""Single-entity reads answer the DTO core returns.

GET /api/v1/notification-providers/{id} still masks secrets, and
GET /api/v1/users/{id} answers the user or 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_notification_provider, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", False)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            c.cookies.set(settings.security.session_cookie_name, token)
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_a_provider_read_masks_its_secret(
    client: AsyncClient, db: AsyncSession
) -> None:
    provider = make_notification_provider(
        config={"post_url": "https://example.test/hook", "token": "s3cret"}
    )
    db.add(provider)
    await db.flush()

    resp = await client.get(f"/api/v1/notification-providers/{provider.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["token"] == "***MASKED***"
    assert resp.json()["config"]["post_url"] == "https://example.test/hook"


async def test_a_user_read_by_id(client: AsyncClient, db: AsyncSession) -> None:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()

    found = await client.get(f"/api/v1/users/{user.id}")
    missing = await client.get(f"/api/v1/users/{uuid4()}")

    assert found.status_code == 200, found.text
    assert found.json()["username"] == user.username
    assert missing.status_code == 404
