"""The component catalogue renders every macro it registers.

The catalogue is where agents discover the macro library. The new swatches
call each macro with the shape its real page passes, so a broken macro or a
stale sample fails here rather than on the page that uses it.
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


@pytest.mark.parametrize(
    "rendered",
    [
        "#components-delete-demo",  # delete_icon_button targets it
        "Delete Job",  # confirm_action_btn
        "ALL",  # segmented_option
        'id="filter-search"',  # filter_search inside filter_card
        'id="filter-status"',  # filter_select
        'id="filter-per_page"',  # filter_per_page
        "New Registration Key",  # panel_header
        "api-health",  # status_row_compressed
        "No history data available",  # latency_chart, empty state
        "Email, webhook and Home Assistant providers",  # settings_category_card
        "TCP port to probe",  # render_field_from_schema
    ],
)
async def test_each_swatch_renders(client: AsyncClient, rendered: str) -> None:
    resp = await client.get("/settings/components")

    assert resp.status_code == 200, resp.text[:500]
    assert rendered in resp.text
