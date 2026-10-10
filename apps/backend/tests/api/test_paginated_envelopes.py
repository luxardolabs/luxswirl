"""The JSON list routes answer with the fleet's one page envelope.

Alerts and notification providers returned `page_size`/`pages`, and users
returned `users`/`page_size` with a page number derived from `skip // limit`.
All three now return `PaginatedResponse`: items, total, page, per_page,
total_pages, has_next, built once by `build_pagination`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_alert, make_notification_provider, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ENVELOPE = {"items", "total", "page", "per_page", "total_pages", "has_next"}


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


@pytest.mark.parametrize(
    ("path", "make"),
    [
        ("/api/v1/alerts", make_alert),
        ("/api/v1/notification-providers", make_notification_provider),
        ("/api/v1/users", make_user),
    ],
)
async def test_each_list_pages_with_the_one_envelope(
    client: AsyncClient, db: AsyncSession, path: str, make
) -> None:
    admin = make_user(role="admin")  # /users answers an admin session only
    db.add_all([admin, *(make() for _ in range(3))])
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    client.cookies.set(settings.security.session_cookie_name, token)
    probe = await client.get(path, params={"per_page": 200})
    assert probe.status_code == 200, probe.text
    total = probe.json()["total"]

    first = await client.get(path, params={"page": 1, "per_page": 2})
    last = await client.get(path, params={"page": -(-total // 2), "per_page": 2})

    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == ENVELOPE
    assert (body["page"], body["per_page"], body["total"]) == (1, 2, total)
    assert body["total_pages"] == -(-total // 2)
    assert len(body["items"]) == 2
    assert body["has_next"] is True
    assert last.json()["has_next"] is False
