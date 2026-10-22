"""The check edit, clone and dependents panels render from CheckView DTOs."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def test_edit_clone_and_dependents_panels(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(agent_name="edge-a")
    db.add(agent)
    await db.flush()
    parent = make_check(agent_id=agent.id, display_name="gateway")
    child = make_check(agent_id=agent.id, display_name="api-behind-gateway")
    db.add_all([parent, child])
    await db.flush()

    paths = {
        "edit": f"/checks/{parent.id}/edit-form",
        "clone": f"/checks/{parent.id}/clone-form",
        "dependents": f"/checks/{parent.id}/dependents",
    }
    for name, path in paths.items():
        resp = await client.get(path)
        assert resp.status_code == 200, (name, resp.text[:300])
        assert "gateway" in resp.text, name
