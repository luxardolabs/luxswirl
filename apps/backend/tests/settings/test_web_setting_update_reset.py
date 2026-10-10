"""Updating and resetting a setting from its card round-trips through DTOs.

The view reads the setting's type from the core's SettingResponse and renders
the card from the SettingResponse update/reset return; nothing in between is
an ORM row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.db.database import get_db
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            c.cookies.set(settings.security.session_cookie_name, token)
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_update_then_reset_an_int_setting(
    client: AsyncClient, db: AsyncSession
) -> None:
    await SettingsCoreService.create_setting(
        db, key="test.card_int", category="system", value=50, display_name="Card Int"
    )

    updated = await client.post("/settings/test.card_int/update", data={"value": "75"})
    assert updated.status_code == 200, updated.text
    assert await SettingsCoreService.get_setting(db, "test.card_int") == 75

    reset = await client.post("/settings/test.card_int/reset")
    assert reset.status_code == 200, reset.text
    assert await SettingsCoreService.get_setting(db, "test.card_int") == 50


async def test_a_value_of_the_wrong_type_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    await SettingsCoreService.create_setting(
        db, key="test.card_bad", category="system", value=5, display_name="Card Bad"
    )

    resp = await client.post("/settings/test.card_bad/update", data={"value": "five"})

    assert await SettingsCoreService.get_setting(db, "test.card_bad") == 5
    assert resp.status_code in (200, 400, 422)
