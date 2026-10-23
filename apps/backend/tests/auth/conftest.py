"""Auth-domain fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def web_signed_in_client(
    db: AsyncSession, monkeypatch
) -> AsyncIterator[AsyncClient]:
    """A browser client signed in by the session cookie, with authentication on
    (what test_logout_revokes.py replays)."""
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as client:
        client.cookies.set(settings.security.session_cookie_name, token)
        yield client
    limiter.reset()
