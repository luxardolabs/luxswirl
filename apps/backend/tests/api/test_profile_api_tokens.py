"""The profile panel creates a user's API token, shows it once, and revokes it.

A token made in the browser opens the admin API as that user; one revoked
there stops; and a user cannot revoke another user's token by its id.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ME = "/api/v1/admin/me"
TOKEN = r"lsw_[A-Za-z0-9_-]{43}"


@pytest_asyncio.fixture
async def browser(
    db: AsyncSession, monkeypatch
) -> AsyncIterator[tuple[AsyncClient, object]]:
    """A browser signed in as a viewer, with authentication on."""
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, session_token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
        headers={"Origin": f"https://{settings.allowed_hosts[0]}"},
    ) as client:
        client.cookies.set(settings.security.session_cookie_name, session_token)
        yield client, user
    limiter.reset()


async def _api_me(token: str) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as api:
        return (
            await api.get(ME, headers={"Authorization": f"Bearer {token}"})
        ).status_code


async def test_a_token_made_in_the_profile_panel_is_shown_once_and_works(
    db: AsyncSession, browser
) -> None:
    client, user = browser

    created = await client.post(
        "/profile/api-tokens", data={"name": "laptop", "expires_in_days": "30"}
    )
    assert created.status_code == 200
    shown = re.findall(TOKEN, created.text)
    assert len(set(shown)) == 1
    plaintext = shown[0]
    assert await _api_me(plaintext) == 200

    panel = await client.get("/profile")
    assert panel.status_code == 200
    assert "laptop" in panel.text
    assert plaintext not in panel.text
    assert plaintext[:12] in panel.text


async def test_a_token_revoked_in_the_profile_panel_stops_working(
    db: AsyncSession, browser
) -> None:
    client, user = browser
    token, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="retired-token")
    )

    revoked = await client.post(f"/profile/api-tokens/{token.id}/revoke")

    assert revoked.status_code == 200
    assert "retired-token" not in revoked.text
    assert await _api_me(plaintext) == 401


async def test_a_user_cannot_revoke_another_users_token_from_the_panel(
    db: AsyncSession, browser
) -> None:
    client, _ = browser
    other = make_user(role="admin")
    db.add(other)
    await db.flush()
    token, plaintext = await ApiTokenCoreService.create_token(
        db, other.id, ApiTokenCreate(name="theirs")
    )

    response = await client.post(f"/profile/api-tokens/{token.id}/revoke")

    assert response.status_code == 200
    assert "no longer exists" in response.text
    assert await _api_me(plaintext) == 200


@pytest.mark.parametrize(
    "form",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"name": "ok", "expires_in_days": "0"},
        {"name": "ok", "expires_in_days": "abc"},
    ],
)
async def test_a_bad_token_form_creates_nothing(
    db: AsyncSession, browser, form: dict[str, str]
) -> None:
    client, user = browser

    response = await client.post("/profile/api-tokens", data=form)

    assert response.status_code in (200, 422)
    assert await ApiTokenCoreService.list_tokens(db, user.id) == []
