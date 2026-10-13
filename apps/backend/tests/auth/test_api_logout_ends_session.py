"""JSON-API logout ends the session on the server, not just in the browser.

`POST /api/v1/auth/logout` deleted the session row only `if session_token`, and
`session_token` was a plain route parameter (a query string FastAPI never fills
from the cookie). So logout cleared the cookie and left the session valid: a
copied cookie kept working. The web logout had the same defect
(test_web_logout_ends_session.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_api_logout_deletes_the_session_it_was_called_with(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    auth = AuthCoreService()
    _, token = await auth.create_session(db, user.id)
    assert await auth.verify_session(db, token) is not None

    client.cookies.set(settings.security.session_cookie_name, token)
    resp = await client.post("/api/v1/auth/logout")

    assert resp.status_code == 200
    assert await auth.verify_session(db, token) is None


async def test_a_copied_cookie_is_refused_after_api_logout(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)

    client.cookies.set(settings.security.session_cookie_name, token)
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    assert (await client.post("/api/v1/auth/logout")).status_code == 200

    # The browser's cookie is cleared; a copy of it must not still work.
    client.cookies.set(settings.security.session_cookie_name, token)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
