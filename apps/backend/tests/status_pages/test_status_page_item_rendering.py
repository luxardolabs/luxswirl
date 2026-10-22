"""Status-page items render from data the view hands each item.

The templates indexed neighbours and parallel lists by loop position:
`rendered_items[loop.index0 - 1]` to decide where a row of check cards opens
and closes, and `remove_item_urls[loop.index0]` for each item's remove link.
The views now put both on the item. These render the real pages.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.status_page_model import StatusPage
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


@pytest_asyncio.fixture
async def page(db: AsyncSession) -> StatusPage:
    """Items: check, group, check, check — two runs of check cards."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(4)]
    db.add_all(checks)
    await db.flush()
    status_page = StatusPage(
        name="Runs",
        slug="runs",
        is_public=True,
        items=[
            {"type": "check", "order": 0, "check_id": str(checks[0].id)},
            {
                "type": "group",
                "order": 1,
                "name": "Core",
                "checks": [str(checks[1].id)],
            },
            {"type": "check", "order": 2, "check_id": str(checks[2].id)},
            {"type": "check", "order": 3, "check_id": str(checks[3].id)},
        ],
    )
    db.add(status_page)
    await db.flush()
    return status_page


async def test_consecutive_checks_share_one_row(
    client: AsyncClient, page: StatusPage
) -> None:
    resp = await client.get(f"/status/{page.slug}")

    assert resp.status_code == 200, resp.text[:500]
    # Runs: [check] and [check, check]; the group between them breaks the run
    # (and draws its own grid, with mt-4).
    assert resp.text.count('class="flex flex-wrap gap-4 card-grid"') == 2


async def test_each_item_removes_itself(client: AsyncClient, page: StatusPage) -> None:
    resp = await client.get(f"/status-pages/{page.id}/manage")

    assert resp.status_code == 200, resp.text[:500]
    removes = re.findall(
        rf"htmx\.ajax\('DELETE', '/status-pages/{page.id}/remove-item/(\d+)'", resp.text
    )
    assert sorted(int(index) for index in removes) == [0, 1, 2, 3]
