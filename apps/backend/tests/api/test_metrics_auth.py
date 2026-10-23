"""/metrics takes its own optional bearer token, and nothing else.

With `metrics.auth_required` on, the only accepted credential is the
`metrics.bearer_token` setting (Settings > API Keys). There is no shared admin
API token any more, so no other bearer opens the scrape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from app.services.core.settings_core_service import SettingsCoreService
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

TOKEN = "metrics-scrape-token"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _metrics_settings(
    db: AsyncSession, *, auth_required: bool, token: str
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(db, "metrics.auth_required", auth_required)
    await SettingsCoreService.update_setting(db, "metrics.bearer_token", token)


async def test_the_metrics_token_opens_the_scrape(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _metrics_settings(db, auth_required=True, token=TOKEN)

    resp = await client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")


async def test_no_token_or_another_token_is_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _metrics_settings(db, auth_required=True, token=TOKEN)

    missing = await client.get("/metrics")
    wrong = await client.get("/metrics", headers={"Authorization": "Bearer other"})
    unprefixed = await client.get("/metrics", headers={"Authorization": TOKEN})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert unprefixed.status_code == 401


async def test_a_non_ascii_bearer_is_refused_not_a_server_error(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _metrics_settings(db, auth_required=True, token=TOKEN)

    resp = await client.get(
        "/metrics", headers={"Authorization": "Bearer caf\xe9".encode("latin-1")}
    )

    assert resp.status_code == 401


async def test_auth_required_with_no_token_configured_opens_to_no_one(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An empty configured token never matches an empty bearer."""
    await _metrics_settings(db, auth_required=True, token="")

    resp = await client.get("/metrics", headers={"Authorization": "Bearer "})

    assert resp.status_code == 401


async def test_the_scrape_is_open_when_auth_is_not_required(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _metrics_settings(db, auth_required=False, token=TOKEN)

    resp = await client.get("/metrics")

    assert resp.status_code == 200


def test_there_is_no_shared_admin_api_token_setting() -> None:
    """No setting holds a shared API token, so nothing can accept one."""
    assert "auth_tokens" not in type(settings.security).model_fields
