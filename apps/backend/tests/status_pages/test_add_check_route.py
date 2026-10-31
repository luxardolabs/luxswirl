"""The editor's add-check names the check it stored, and refuses one that does not exist.

The manage page marks an available check's card as added from the response's
HX-Trigger `statusPageCheckAdded` event, so only a stored check is marked,
whatever else is in flight.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.schemas.status_page_schema import StatusPageCreate
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HX = {"HX-Request": "true"}


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def _add(client: AsyncClient, page_id: str, check_id: str):
    return await client.post(
        app.url_path_for("status_page_items.add_check", status_page_id=page_id),
        data={"check_id": check_id},
        headers=HX,
    )


async def test_an_added_check_is_named_in_the_response(
    client: AsyncClient, db: AsyncSession
) -> None:
    page = await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="page", slug=f"p-{uuid4().hex[:8]}")
    )
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()

    resp = await _add(client, str(page.id), str(check.id))

    assert resp.status_code == 200
    events = json.loads(resp.headers["HX-Trigger"])
    assert events["statusPageCheckAdded"] == {"checkId": str(check.id)}
    assert events["showToast"]["type"] == "success"


async def test_a_check_that_does_not_exist_is_refused_and_not_named(
    client: AsyncClient, db: AsyncSession
) -> None:
    page = await StatusPageCoreService.create_status_page(
        db, StatusPageCreate(name="page", slug=f"p-{uuid4().hex[:8]}")
    )

    resp = await _add(client, str(page.id), str(uuid4()))

    assert resp.status_code == 400
    assert "statusPageCheckAdded" not in resp.headers.get("HX-Trigger", "")
    stored = await StatusPageCoreService.get_status_page(db, page.id)
    assert stored is not None
    assert stored.items == []
