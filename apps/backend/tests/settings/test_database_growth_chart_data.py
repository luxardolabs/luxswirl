"""The database-growth chart's data is an admin web fragment the chart script parses.

The page renders the fragment's URL into the canvas (`data-url`, resolved by
route name); `static/js/database-health.js` fetches it and reads the series
from `#database-growth-data[data-points]`. A viewer never gets the data.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

POINT_KEYS = {
    "timestamp",
    "results_mb",
    "artifacts_mb",
    "checks_mb",
    "agent_metrics_mb",
    "total_mb",
}


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        limiter.reset()


async def _sign_in(client: AsyncClient, db: AsyncSession, role: str) -> None:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)


def _points(body: str) -> list[dict[str, object]]:
    match = re.search(r'id="database-growth-data" data-points="([^"]*)"', body)
    assert match, body[:300]
    return json.loads(html.unescape(match.group(1)))


async def test_an_admin_gets_the_points_in_the_fragment(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    db.add(make_check_result(check_id=check.id, agent_id=agent.id, timestamp=utc_now()))
    await db.flush()
    await _sign_in(client, db, role="admin")

    resp = await client.get(
        app.url_path_for("database_health.chart_data"), params={"hours": 24}
    )

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"].startswith("text/html")
    points = _points(resp.text)
    assert points, "a result in the window yields at least one bucket"
    assert all(set(p) == POINT_KEYS for p in points)


async def test_the_page_points_the_chart_at_the_fragment(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db, role="admin")

    page = await client.get(app.url_path_for("database_health.index"))

    assert page.status_code == 200
    assert f'data-url="{app.url_path_for("database_health.chart_data")}"' in page.text


async def test_an_out_of_range_window_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db, role="admin")

    resp = await client.get(
        app.url_path_for("database_health.chart_data"), params={"hours": 0}
    )

    assert resp.status_code == 422


async def test_a_viewer_does_not_get_the_data(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db, role="viewer")

    resp = await client.get(app.url_path_for("database_health.chart_data"))

    assert resp.status_code == 307
    assert "database-growth-data" not in resp.text
