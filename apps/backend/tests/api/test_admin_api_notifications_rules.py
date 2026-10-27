"""What the admin API holds a notification provider to, past its body's shape.

- A provider created disabled is stored disabled.
- A rate limit, once set, can be removed with null.
- A Home Assistant endpoint is held to the webhook's network policy: no cloud
  metadata address, at config time or when a test is sent.
- A delivery error that quotes a header value (a token) is stored and served
  with the token masked.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.notifications.providers import homeassistant as ha_mod
from app.notifications.providers.base import NotificationContext
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_alert, make_notification_provider, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PROVIDERS = "/api/v1/admin/notification-providers"
LOGS = "/api/v1/admin/notification-logs"
HDR_SECRET = "hdr-s3cret-value"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role)
    )
    return {"Authorization": f"Bearer {plaintext}"}


# ── 1. is_enabled on create ─────────────────────────────────────────────────


async def test_create_disabled_is_stored_disabled(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    resp = await client.post(
        PROVIDERS,
        json={
            "provider_type": "webhook",
            "friendly_name": "off hook",
            "config": {"post_url": "https://example.test/hook"},
            "is_enabled": False,
        },
        headers=editor,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_enabled"] is False, resp.json()


async def test_control_create_default_enabled(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    resp = await client.post(
        PROVIDERS,
        json={
            "provider_type": "webhook",
            "friendly_name": "on hook",
            "config": {"post_url": "https://example.test/hook"},
            "is_default_enabled": True,
        },
        headers=editor,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_enabled"] is True
    assert resp.json()["is_default_enabled"] is True


# ── 2. rate limit can never be cleared ──────────────────────────────────────


async def test_rate_limit_can_be_removed(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    provider = make_notification_provider(rate_limit_count=5)
    db.add(provider)
    await db.flush()
    resp = await client.patch(
        f"{PROVIDERS}/{provider.id}", json={"rate_limit_count": None}, headers=editor
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rate_limit_count"] is None


# ── 3. Home Assistant provider to cloud metadata ────────────────────────────


@pytest.mark.parametrize("ptype", ["webhook", "homeassistant"])
async def test_metadata_url_refused(
    db: AsyncSession, client: AsyncClient, ptype: str
) -> None:
    editor = await _as(db, "editor")
    resp = await client.post(
        PROVIDERS,
        json={
            "provider_type": ptype,
            "friendly_name": f"meta {ptype}",
            "config": {"post_url": "http://169.254.169.254/api/webhook/latest"},
        },
        headers=editor,
    )
    assert resp.status_code in (400, 422), (ptype, resp.status_code, resp.text)


async def test_ha_test_route_posts_to_metadata(
    db: AsyncSession, client: AsyncClient, monkeypatch
) -> None:
    editor = await _as(db, "editor")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)

    def make_client(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(
        ha_mod,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=make_client, HTTPStatusError=httpx.HTTPStatusError
        ),
    )
    provider = make_notification_provider(
        provider_type="homeassistant",
        config={"post_url": "http://169.254.169.254/api/webhook/x"},
    )
    db.add(provider)
    await db.flush()
    resp = await client.post(f"{PROVIDERS}/{provider.id}/test", headers=editor)
    assert resp.status_code == 200, resp.text
    assert seen == [], f"request reached {seen}; response {resp.json()}"


# ── 4. a header value echoed into the viewer-readable log ───────────────────


async def test_bad_header_value_leaks_into_log(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")

    async def _serve(reader, writer):
        try:
            await reader.read(1024)
        finally:
            writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        provider = make_notification_provider(
            provider_type="webhook",
            config={
                "post_url": f"http://127.0.0.1:{port}/hook",
                "additional_headers": {"Authorization": f"Bearer {HDR_SECRET}\n"},
            },
        )
        alert = make_alert()
        db.add_all([provider, alert])
        await db.flush()

        tested = await client.post(f"{PROVIDERS}/{provider.id}/test", headers=editor)

        ctx = NotificationContext(
            check_name="c",
            check_type="http",
            target="t",
            agent_id=None,
            status="error",
        )
        rows = await NotificationCoreService.send_notifications(
            db,
            [provider.id],
            ctx,
            alert_id=alert.id,
            check_result_id=None,
            check_result_timestamp=None,
        )
        await db.flush()
        logs = await client.get(
            LOGS, params={"alert_id": str(alert.id)}, headers=viewer
        )
    finally:
        server.close()
        await server.wait_closed()

    assert logs.status_code == 200
    assert HDR_SECRET not in logs.text, logs.text
    assert HDR_SECRET not in tested.text, tested.text
    assert all(HDR_SECRET not in (row.error_message or "") for row in rows)
