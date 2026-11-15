"""A user who must still change their password reads a private status page as
anyone signed out does: sent to sign in, never served the page.

The forced change comes before any other page (FLEET-ADMIN-BOOTSTRAP-STANDARD
§5); the public status page view reads the optional user, so it treats one
with `must_change_password` as anonymous.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        limiter.reset()


async def _sign_in(db: AsyncSession, client: AsyncClient, *, must_change: bool) -> None:
    user = make_user(role="viewer", must_change_password=must_change)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)


async def _private_page(db: AsyncSession, slug: str) -> StatusPage:
    page = StatusPage(
        name="Private",
        slug=slug,
        is_public=False,
        items=[],
        config={"status_bar_minutes": 60},
    )
    db.add(page)
    await db.flush()
    return page


async def test_a_user_who_must_change_their_password_is_sent_to_sign_in(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(db, client, must_change=True)
    page = await _private_page(db, "private-must-change")

    resp = await client.get(f"/status/{page.slug}")

    assert resp.status_code == 307, "private page served before the forced change"


async def test_their_partial_refresh_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(db, client, must_change=True)
    page = await _private_page(db, "private-must-change-partial")

    resp = await client.get(f"/status/{page.slug}/partial")

    assert resp.status_code == 401


async def test_control_a_signed_in_user_reads_the_private_page(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(db, client, must_change=False)
    page = await _private_page(db, "private-signed-in")

    assert (await client.get(f"/status/{page.slug}")).status_code == 200
    assert (await client.get(f"/status/{page.slug}/partial")).status_code == 200


async def test_control_the_change_password_page_still_opens(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(db, client, must_change=True)

    resp = await client.get("/change-password", params={"required": "true"})

    assert resp.status_code == 200
    assert 'name="new_password"' in resp.text
