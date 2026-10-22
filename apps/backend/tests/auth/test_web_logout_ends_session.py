"""Web logout ends the session on the server, not just in the browser.

`GET/POST /logout` only deleted the session row `if user and session_token`,
and `session_token` was a plain route parameter (a query string FastAPI never
fills from the cookie), read under a hard-coded cookie name besides. So logout
cleared the cookie and left the session valid: a copied cookie kept working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        yield c


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_logout_deletes_the_session_it_was_called_with(
    client: AsyncClient, db: AsyncSession, method: str
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    auth = AuthCoreService()
    _, token = await auth.create_session(db, user.id)
    assert await auth.verify_session(db, token) is not None

    client.cookies.set(settings.security.session_cookie_name, token)
    resp = await client.request(method, "/logout")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    assert await auth.verify_session(db, token) is None
