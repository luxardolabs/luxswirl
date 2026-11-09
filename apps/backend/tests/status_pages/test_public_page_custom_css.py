"""A public status page writes nothing from its stored config into a style block.

The public layout rendered `config.custom_css | safe` inside `<style>`, so a
stored value holding `</style>` closed the block and wrote markup onto an
unauthenticated page. No route writes that key today, but a stored row (an old
import, a direct write, a later config field) reached the sink. The page now
renders no part of its config as CSS, and carries no inline style block.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.main import app
from app.models.status_page_model import StatusPage
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PAYLOAD = '</style><div id="pwned" x-init="alert(1)"></div><style>'
MARKER = "body{background:#c0ffee}"


@pytest_asyncio.fixture
async def anonymous(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        yield c


async def test_a_stored_custom_css_cannot_break_out_of_the_page(
    anonymous: AsyncClient, db: AsyncSession
) -> None:
    page = StatusPage(
        name="Public",
        slug="public-css",
        is_public=True,
        items=[],
        config={"status_bar_minutes": 60, "custom_css": MARKER + PAYLOAD},
    )
    db.add(page)
    await db.flush()

    resp = await anonymous.get(f"/status/{page.slug}")

    assert resp.status_code == 200, resp.text[:500]
    assert 'id="pwned"' not in resp.text
    assert MARKER not in resp.text
    assert "<style" not in resp.text
