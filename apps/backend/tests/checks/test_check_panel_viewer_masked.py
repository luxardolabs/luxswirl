"""The check detail panel shows a viewer a result's error and metrics masked.

The admin API does the same for the same viewer (`AdminCheckResultCoreService`). A
monitored system's error text can quote a connection string, and a synthetic check's
console errors can quote what the page logged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET = "S3cretZZ9"
ERROR = f"could not connect to postgresql://monitor:{SECRET}@db.internal:5432/app"
METRICS = json.dumps(
    {
        "synthetic": {
            "status": "failed",
            "console_errors": [
                f"fetch failed https://monitor:{SECRET}@app.example.com/"
            ],
        }
    }
)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c


async def _panel(
    client: AsyncClient, db: AsyncSession, check_type: str, role: str
) -> str:
    agent = make_agent()
    user = make_user(role=role)
    db.add_all([agent, user])
    await db.flush()
    check = make_check(agent_id=agent.id, check_type=check_type)
    db.add(check)
    await db.flush()
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=False,
            error=ERROR,
            metrics=METRICS,
        )
    )
    await db.flush()
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.parametrize(
    ("check_type", "still_shown"),
    [("http", "could not connect to"), ("synthetic", "app.example.com")],
)
async def test_a_viewer_reads_the_panel_masked(
    client: AsyncClient, db: AsyncSession, check_type: str, still_shown: str
) -> None:
    panel = await _panel(client, db, check_type, "viewer")

    assert still_shown in panel
    assert SECRET not in panel


async def test_an_editor_reads_the_error_as_it_was_reported(
    client: AsyncClient, db: AsyncSession
) -> None:
    panel = await _panel(client, db, "http", "editor")

    assert SECRET in panel
