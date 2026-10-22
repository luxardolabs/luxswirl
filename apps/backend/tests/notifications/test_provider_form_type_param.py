"""The provider form's `provider_type` is validated as the enum at the boundary.

It was a bare `str`, so any value reached the view and rendered a form with no
config fields. An unknown type is now a 422; empty still means "pick a type".
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

FORM = "/notification-providers/create-form"


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    user = make_user(role="admin")
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


@pytest.mark.parametrize("query", ["", "?provider_type=", "?provider_type=webhook"])
async def test_known_or_empty_type_renders(client: AsyncClient, query: str) -> None:
    assert (await client.get(FORM + query)).status_code == 200


async def test_unknown_type_is_refused(client: AsyncClient) -> None:
    assert (await client.get(FORM + "?provider_type=pager")).status_code == 422
