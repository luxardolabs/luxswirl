"""Breaks an adversarial pass found in the admin-bootstrap and auth onboarding.

1. An env file that still set the retired SECURITY__INITIAL_ADMIN_* crashed the server at
   import (SecuritySettings forbade unknown keys).
2. The recovery reset left an owner locked out by failed logins still locked out.
3. A seeded or reset user (must_change_password) could open any page after login
   instead of being held at the change form.
4. A cross-site GET /logout (a link or image on another site) signed the user out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.config import Settings, settings
from app.core.datetime_utils import utc_now
from app.db.database import get_db
from app.main import app
from app.services.core.admin_bootstrap_core_service import AdminBootstrapCoreService
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
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_a_retired_initial_admin_key_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURITY__INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SECURITY__INITIAL_ADMIN_PASSWORD", "old-seed-password")

    assert Settings(_env_file=None).allowed_hosts


async def test_recovery_reset_lets_a_locked_out_owner_back_in(db: AsyncSession) -> None:
    user = make_user(
        username="owner",
        failed_login_attempts=5,
        locked_until=utc_now() + timedelta(minutes=30),
    )
    db.add(user)
    await db.flush()

    temporary = await AdminBootstrapCoreService().reset_password(db, "owner")

    signed_in = await AuthCoreService().authenticate_user(db, "owner", temporary)
    assert signed_in is not None


@pytest.mark.parametrize("page", ["/agents", "/settings"])
async def test_a_user_who_must_change_the_password_is_held_at_the_form(
    client: AsyncClient, db: AsyncSession, page: str
) -> None:
    user = make_user(must_change_password=True)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)

    r = await client.get(page)

    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("/change-password")
    assert (await client.get("/change-password")).status_code == 200


async def test_a_cross_site_get_logout_keeps_the_session(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    auth = AuthCoreService()
    _, token = await auth.create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)

    r = await client.get("/logout", headers={"Sec-Fetch-Site": "cross-site"})

    assert r.status_code == 403
    assert await auth.verify_session(db, token) is not None


async def test_a_same_site_get_logout_still_ends_the_session(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    auth = AuthCoreService()
    _, token = await auth.create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)

    r = await client.get("/logout", headers={"Sec-Fetch-Site": "same-origin"})

    assert r.status_code == 302
    assert await auth.verify_session(db, token) is None
