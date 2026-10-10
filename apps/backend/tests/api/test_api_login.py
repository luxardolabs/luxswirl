"""`POST /api/v1/auth/login` signs a valid user in.

It passed the User row to `create_session`, which takes the user's id and
re-reads the user with it, so the lookup ran on an ORM object and a valid
JSON-API login never produced a session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        limiter.reset()


async def test_a_valid_login_returns_the_user_and_a_session_cookie(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(make_user(username="ops"))
    await db.flush()

    resp = await client.post(
        "/api/v1/auth/login", json={"username": "ops", "password": "TestPass123!"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["username"] == "ops"
    assert settings.security.session_cookie_name in resp.cookies


async def test_a_wrong_password_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(make_user(username="ops2"))
    await db.flush()

    resp = await client.post(
        "/api/v1/auth/login", json={"username": "ops2", "password": "nope"}
    )

    assert resp.status_code == 401


async def test_a_required_password_change_is_flagged_and_issues_no_session(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(make_user(username="ops3", must_change_password=True))
    await db.flush()

    resp = await client.post(
        "/api/v1/auth/login", json={"username": "ops3", "password": "TestPass123!"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is True
    assert settings.security.session_cookie_name not in resp.cookies
