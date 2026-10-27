"""Notification providers and the notification log on the admin API.

A viewer's token reads providers without their configs, where the
credentials are; an editor's reads and changes them. The log is anyone's to
read, its errors with each URL cut to the host and its check targets masked.
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.notifications.providers import webhook as hook_mod
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_check,
    make_check_result,
    make_notification_log,
    make_notification_provider,
    make_user,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PROVIDERS = "/api/v1/admin/notification-providers"
LOGS = "/api/v1/admin/notification-logs"
HOOK = "https://hooks.example.test/api/webhook/hook-id-s3cret"
SMTP_PW = "smtp-s3cret"


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
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _email(db: AsyncSession):
    provider = make_notification_provider(
        provider_type="email",
        config={
            "hostname": "smtp.example.test",
            "port": 587,
            "security": "starttls",
            "username": "alerts",
            "password": SMTP_PW,
            "from_email": "alerts@example.test",
            "to_email": "ops@example.test",
        },
    )
    db.add(provider)
    await db.flush()
    return provider


# ── Providers: who reads what ────────────────────────────────────────────────


async def test_a_viewer_reads_providers_without_their_config(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")
    provider = await _email(db)

    listed = await client.get(PROVIDERS, params={"per_page": 200}, headers=viewer)
    one = await client.get(f"{PROVIDERS}/{provider.id}", headers=viewer)
    full = await client.get(f"{PROVIDERS}/{provider.id}", headers=editor)

    assert listed.status_code == 200, listed.text
    assert SMTP_PW not in listed.text and SMTP_PW not in one.text
    assert one.json()["config"] is None
    assert one.json()["friendly_name"] == provider.friendly_name
    assert full.json()["config"]["password"] == SMTP_PW


async def test_the_provider_types_name_their_config_fields(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")

    resp = await client.get(f"{PROVIDERS}/types", headers=viewer)

    types_by_name = {t["type"]: t for t in resp.json()}
    assert {"email", "webhook", "homeassistant"} <= types_by_name.keys()
    assert "password" in types_by_name["email"]["config_schema"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            "",
            {
                "provider_type": "webhook",
                "friendly_name": "x",
                "config": {"post_url": HOOK},
            },
        ),
        ("PATCH", "/{id}", {"friendly_name": "y"}),
        ("DELETE", "/{id}", None),
        ("POST", "/{id}/enable", None),
        ("POST", "/{id}/disable", None),
        ("POST", "/{id}/test", None),
    ],
)
async def test_a_viewer_cannot_change_a_provider(
    db: AsyncSession,
    client: AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    viewer = await _as(db, "viewer")
    provider = await _email(db)

    resp = await client.request(
        method, PROVIDERS + path.format(id=provider.id), json=body, headers=viewer
    )

    assert resp.status_code == 403, resp.text


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(PROVIDERS)).status_code == 401
    assert (await client.get(LOGS)).status_code == 401


# ── Providers: changes ───────────────────────────────────────────────────────


async def test_an_editor_creates_edits_disables_and_deletes_a_provider(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")

    created = await client.post(
        PROVIDERS,
        json={
            "provider_type": "webhook",
            "friendly_name": "ops hook",
            "config": {"post_url": HOOK},
        },
        headers=editor,
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    edited = await client.patch(
        f"{PROVIDERS}/{pid}", json={"friendly_name": "ops hook 2"}, headers=editor
    )
    off = await client.post(f"{PROVIDERS}/{pid}/disable", headers=editor)
    gone = await client.delete(f"{PROVIDERS}/{pid}", headers=editor)
    after = await client.get(f"{PROVIDERS}/{pid}", headers=editor)

    assert edited.json()["friendly_name"] == "ops hook 2"
    assert edited.json()["config"] == {"post_url": HOOK}
    assert off.json()["is_enabled"] is False
    assert (gone.status_code, after.status_code) == (204, 404)


@pytest.mark.parametrize(
    "body",
    [
        {"provider_type": "carrier-pigeon", "friendly_name": "x", "config": {}},
        {"provider_type": "webhook", "friendly_name": "x", "config": {}},
        {
            "provider_type": "webhook",
            "friendly_name": "x",
            "config": {"post_url": HOOK},
            "deleted_at": None,
        },
    ],
)
async def test_a_bad_provider_is_refused(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    editor = await _as(db, "editor")

    resp = await client.post(PROVIDERS, json=body, headers=editor)

    assert resp.status_code in (400, 422), resp.text


async def test_an_edit_refuses_null(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    provider = await _email(db)

    resp = await client.patch(
        f"{PROVIDERS}/{provider.id}", json={"config": None}, headers=editor
    )

    assert resp.status_code == 422, resp.text


def _answering(status: int) -> types.SimpleNamespace:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))

    def make_client(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    return types.SimpleNamespace(
        AsyncClient=make_client,
        HTTPStatusError=httpx.HTTPStatusError,
        RequestError=httpx.RequestError,
        TimeoutException=httpx.TimeoutException,
    )


@pytest.mark.parametrize(("status", "success"), [(200, True), (404, False)])
async def test_a_provider_test_reports_its_outcome_without_the_url(
    db: AsyncSession, client: AsyncClient, monkeypatch, status: int, success: bool
) -> None:
    editor = await _as(db, "editor")
    monkeypatch.setattr(hook_mod, "httpx", _answering(status))
    provider = make_notification_provider(
        provider_type="webhook", config={"post_url": HOOK}
    )
    db.add(provider)
    await db.flush()

    resp = await client.post(f"{PROVIDERS}/{provider.id}/test", headers=editor)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is success
    assert "hook-id-s3cret" not in resp.text


# ── The notification log ─────────────────────────────────────────────────────


async def test_the_log_is_read_by_any_token_with_secrets_masked(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    provider = await _email(db)
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="postgres",
        target="postgresql://monitor:db-s3cret@db.internal:5432/app",
    )
    alert = make_alert()
    db.add_all([check, alert])
    await db.flush()
    result = make_check_result(check_id=check.id, agent_id=agent.id)
    db.add(result)
    await db.flush()
    db.add(
        make_notification_log(
            alert_id=alert.id,
            notification_provider_id=provider.id,
            check_result_id=result.id,
            check_result_timestamp=result.timestamp,
            status="failed",
            error_message=f"Client error '404 Not Found' for url '{HOOK}'",
            sent_at=utc_now(),
        )
    )
    await db.flush()

    resp = await client.get(
        LOGS, params={"alert_id": str(alert.id), "status": "failed"}, headers=viewer
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["error_message"].endswith("for url 'https://hooks.example.test/...'")
    assert row["check_target"] in (None, "postgresql://***@db.internal:5432/app")
    assert "hook-id-s3cret" not in resp.text and "db-s3cret" not in resp.text


async def test_the_log_filters_read_empty_as_absent(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")

    resp = await client.get(
        LOGS,
        params={"status": "", "alert_id": "", "notification_provider_id": ""},
        headers=viewer,
    )

    assert resp.status_code == 200, resp.text


async def test_an_unknown_provider_is_404(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")

    assert (
        await client.get(f"{PROVIDERS}/{uuid4()}", headers=viewer)
    ).status_code == 404
