"""The editor's available-checks list knows which checks the page already names.

The list is re-rendered on every filter change; a check the page names, at
the top level or in a container group, reads "Already on dashboard", and the
page refuses to take it a second time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HX = {"HX-Request": "true"}
ADDED = 'title="Already on dashboard"'


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


async def _check_id(db: AsyncSession, name: str) -> str:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, display_name=name)
    db.add(check)
    await db.flush()
    return str(check.id)


async def _page(db: AsyncSession, items: list[dict[str, Any]]) -> UUID:
    for order, item in enumerate(items):
        item["order"] = order
    page = StatusPage(name="page", slug=f"p-{uuid4().hex[:8]}", items=items)
    db.add(page)
    await db.flush()
    return page.id


def _card(html: str, check_id: str) -> str:
    start = html.index(f'data-check-id="{check_id}"')
    end = html.find('data-check-id="', start + 1)
    return html[start : end if end != -1 else len(html)]


async def test_a_filtered_list_marks_the_checks_the_page_names(
    client: AsyncClient, db: AsyncSession
) -> None:
    top = await _check_id(db, f"top-{uuid4().hex[:6]}")
    grouped = await _check_id(db, f"grouped-{uuid4().hex[:6]}")
    other = await _check_id(db, f"other-{uuid4().hex[:6]}")
    page_id = await _page(
        db,
        [
            {"type": "check", "check_id": top},
            {"type": "group", "name": "g", "checks": [grouped]},
        ],
    )

    resp = await client.get(
        app.url_path_for(
            "status_page_items.available_checks", status_page_id=str(page_id)
        ),
        headers=HX,
    )

    assert resp.status_code == 200
    assert ADDED in _card(resp.text, top)
    assert ADDED in _card(resp.text, grouped)
    assert ADDED not in _card(resp.text, other)


@pytest.mark.parametrize("where", ["check", "group"])
async def test_a_check_the_page_names_cannot_be_added_again(
    db: AsyncSession, where: str
) -> None:
    check_id = await _check_id(db, f"c-{uuid4().hex[:6]}")
    item: dict[str, Any] = (
        {"type": "check", "check_id": check_id}
        if where == "check"
        else {"type": "group", "name": "g", "checks": [check_id]}
    )
    page_id = await _page(db, [item])

    with pytest.raises(ValueError, match="already on the page"):
        await StatusPageCoreService.add_check_to_page(db, page_id, UUID(check_id))

    page = await StatusPageCoreService.get_status_page(db, page_id)
    assert page is not None
    assert len(page.items) == 1


async def test_a_full_page_says_the_check_is_already_on_it(db: AsyncSession) -> None:
    check_id = await _check_id(db, f"c-{uuid4().hex[:6]}")
    page_id = await _page(
        db,
        [{"type": "check", "check_id": check_id}]
        + [{"type": "group", "name": f"g{i}", "checks": []} for i in range(499)],
    )

    with pytest.raises(ValueError, match="already on the page"):
        await StatusPageCoreService.add_check_to_page(db, page_id, UUID(check_id))
