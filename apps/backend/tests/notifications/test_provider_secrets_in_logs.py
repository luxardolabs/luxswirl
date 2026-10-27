"""A refused notification never stores, or shows, its endpoint's URL.

An HTTP library's error names the full request URL, and a notification
endpoint's path can be its credential (a Home Assistant webhook id, a chat
hook's token). A delivery the endpoint refuses is logged with its status
alone, and the notification log page (any signed-in user's) shows every
error with each URL cut to its host, a row written before this included.
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.credential_mask import mask_url_paths
from app.core.rate_limit import limiter
from app.main import app
from app.notifications.providers import homeassistant as ha_mod
from app.notifications.providers import webhook as hook_mod
from app.notifications.providers.base import NotificationContext
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import (
    make_alert,
    make_notification_log,
    make_notification_provider,
    make_user,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HA_URL = "https://10.0.0.5/api/webhook/ha-id-s3cret"
HOOK_URL = "https://10.0.0.6/hooks/T000/B000/slack-tok-s3cret"
SECRETS = ("ha-id-s3cret", "slack-tok-s3cret")


async def _browser(db: AsyncSession, role: str) -> AsyncClient:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    )
    client.cookies.set(settings.security.session_cookie_name, token)
    return client


def _refusing_httpx(status: int) -> types.SimpleNamespace:
    """An httpx stand-in whose endpoint answers `status` to every POST."""
    transport = httpx.MockTransport(lambda request: httpx.Response(status))

    def client(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    return types.SimpleNamespace(
        AsyncClient=client,
        HTTPStatusError=httpx.HTTPStatusError,
        RequestError=httpx.RequestError,
        TimeoutException=httpx.TimeoutException,
    )


@pytest_asyncio.fixture
async def setup(db: AsyncSession, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    monkeypatch.setattr(ha_mod, "httpx", _refusing_httpx(404))
    monkeypatch.setattr(hook_mod, "httpx", _refusing_httpx(500))
    ha = make_notification_provider(
        provider_type="homeassistant", config={"post_url": HA_URL}
    )
    hook = make_notification_provider(
        provider_type="webhook", config={"post_url": HOOK_URL}
    )
    alert = make_alert()
    db.add_all([ha, hook, alert])
    await db.flush()
    context = NotificationContext(
        check_name="web",
        check_type="http",
        target="https://site.example.test",
        agent_id=None,
        status="error",
        alert_name=alert.name,
    )
    rows = await NotificationCoreService.send_notifications(
        db,
        [ha.id, hook.id],
        context,
        alert_id=alert.id,
        check_result_id=None,
        check_result_timestamp=None,
    )
    await db.flush()
    viewer = await _browser(db, "viewer")
    yield {"rows": rows, "viewer": viewer, "alert": alert, "ha": ha}
    await viewer.aclose()
    limiter.reset()


async def test_a_refused_delivery_stores_its_status_not_its_url(setup: dict) -> None:
    errors = sorted(row.error_message or "" for row in setup["rows"])

    assert all(row.status == "failed" for row in setup["rows"]), errors
    assert errors == ["HTTP 404 Not Found", "HTTP 500 Internal Server Error"]


async def test_the_logs_page_shows_no_endpoint_secret(
    db: AsyncSession, setup: dict
) -> None:
    # A row stored before deliveries were logged URL-free.
    db.add(
        make_notification_log(
            alert_id=setup["alert"].id,
            notification_provider_id=setup["ha"].id,
            check_result_id=None,
            check_result_timestamp=None,
            status="failed",
            error_message=f"Client error '404 Not Found' for url '{HA_URL}'",
        )
    )
    await db.flush()

    response = await setup["viewer"].get("/notification-logs")

    assert response.status_code == 200
    for secret in SECRETS:
        assert secret not in response.text, secret
    assert "https://10.0.0.5/..." in response.text


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            f"Client error '404 Not Found' for url '{HA_URL}'",
            "Client error '404 Not Found' for url 'https://10.0.0.5/...'",
        ),
        ("https://u:pw@hook.example/x?token=t", "https://***@hook.example/..."),
        ("connect to https://ha.lan failed", "connect to https://ha.lan/... failed"),
        ("no url here", "no url here"),
        (None, None),
    ],
)
def test_mask_url_paths(text: str | None, shown: str | None) -> None:
    assert mask_url_paths(text) == shown
