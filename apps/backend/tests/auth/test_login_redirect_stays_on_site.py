"""The login page's `redirect` never sends a signed-in user to another site.

`GET /login?redirect=…` (already signed in) and a successful `POST /login` redirected
to the `redirect` value as given, so `/login?redirect=//evil.example` was an open
redirect off a genuine LuxSwirl link. Both now go through the one safe-redirect
helper. FLEET-AUTH-SESSION-STANDARD §6.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

FOREIGN = [
    "//evil.example",
    "https://evil.example/x",
    "/\\evil.example",
    "/%2F%2Fevil.example",
    "javascript:alert(1)",
]


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    # Each test logs in for real: start from an empty login budget, and leave one.
    limiter.reset()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        limiter.reset()


@pytest.mark.parametrize("target", FOREIGN)
async def test_signed_in_login_page_stays_on_site(
    client: AsyncClient, db: AsyncSession, target: str
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)

    r = await client.get("/login", params={"redirect": target})

    assert r.status_code == 302
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("target", FOREIGN)
async def test_successful_login_stays_on_site(
    client: AsyncClient, db: AsyncSession, target: str
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()

    r = await client.post(
        "/login",
        data={
            "username": user.username,
            "password": "TestPass123!",
            "redirect": target,
        },
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_an_on_site_redirect_is_kept(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()

    r = await client.post(
        "/login",
        data={
            "username": user.username,
            "password": "TestPass123!",
            "redirect": "/checks?page=2",
        },
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/checks?page=2"
