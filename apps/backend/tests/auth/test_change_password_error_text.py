"""A refused password change shows the rule it missed, never the typed password.

A form value the request schema refuses renders each error's own message: never
pydantic's whole text, which quotes the value (`input_value='...'`) and links
pydantic's documentation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

RIGHT = "TestPass123!"


@pytest_asyncio.fixture
async def web_client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        limiter.reset()


async def _change(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch, new: str
):
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)
    return await client.post(
        "/change-password",
        data={"current_password": RIGHT, "new_password": new, "confirm_password": new},
    )


def _no_echo(text: str, secret: str) -> None:
    assert "input_value" not in text
    assert "errors.pydantic.dev" not in text
    assert "validation error" not in text
    assert secret not in text


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("Qz7k", "at least 8 characters"),
        ("lowercase9x", "uppercase letter"),
        ("Qz7" + "k" * 120, "at most 100 characters"),
    ],
    ids=["too-short", "no-uppercase", "too-long"],
)
async def test_a_refused_new_password_shows_the_rule_not_the_password(
    web_client: AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    message: str,
) -> None:
    resp = await _change(web_client, db, monkeypatch, secret)
    assert resp.status_code == 400
    _no_echo(resp.text, secret)
    assert message in resp.text


async def test_control_a_mismatch_is_a_plain_message(
    web_client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    web_client.cookies.set(settings.security.session_cookie_name, token)
    resp = await web_client.post(
        "/change-password",
        data={
            "current_password": RIGHT,
            "new_password": "Qz7aaaaaaaaa",
            "confirm_password": "Qz7bbbbbbbbb",
        },
    )
    assert resp.status_code == 400
    assert "do not match" in resp.text
    _no_echo(resp.text, "Qz7aaaaaaaaa")
