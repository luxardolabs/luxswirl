"""The first-run setup gate: /login hands over to /setup until an admin exists,
and /setup closes (to /login) once one does or one is configured via env."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
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
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def no_env_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.security, "initial_admin_password", "")


@pytest.mark.usefixtures("no_env_admin")
async def test_login_sends_a_fresh_install_to_setup(client: AsyncClient) -> None:
    resp = await client.get("/login")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup"


@pytest.mark.usefixtures("no_env_admin")
async def test_login_renders_once_an_admin_exists(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(make_user(role="admin"))
    await db.flush()

    resp = await client.get("/login")

    assert resp.status_code == 200


@pytest.mark.usefixtures("no_env_admin")
async def test_setup_renders_while_no_admin_exists(client: AsyncClient) -> None:
    resp = await client.get("/setup")

    assert resp.status_code == 200


@pytest.mark.usefixtures("no_env_admin")
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_setup_is_closed_once_an_admin_exists(
    client: AsyncClient, db: AsyncSession, method: str
) -> None:
    db.add(make_user(role="admin"))
    await db.flush()

    resp = await client.request(
        method,
        "/setup",
        data={"username": "x", "password": "p", "confirm_password": "p"},
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_setup_is_closed_when_the_admin_comes_from_env(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "initial_admin_password", "from-env")

    resp = await client.get("/setup")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
