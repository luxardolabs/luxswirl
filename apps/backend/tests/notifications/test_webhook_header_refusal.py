"""A webhook's additional headers are held to what HTTP can send.

- A header name that is not an HTTP token, a value that is not a string, or a
  value holding a CR, LF, another control character, a non-ASCII character or
  a leading or trailing space is refused when the provider is saved, by the
  admin API (create and PATCH) and the web form, as a 422 that names the
  header and never repeats its value. A NUL is refused earlier, by the body's
  schema, as text no column can store.
- A provider saved before that refusal existed still fails to send, but before
  any connection is opened, and the failure is recorded (delivery and test
  send) as the app's own refusal naming the header, never the value.
"""

from __future__ import annotations

import json
import types
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import NotificationStatus
from app.notifications.providers import webhook as hook_mod
from app.notifications.providers.base import NotificationContext
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.notification_core_service import NotificationCoreService
from fixtures.factories import make_alert, make_notification_provider, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PROVIDERS = "/api/v1/admin/notification-providers"
URL = "https://example.test/hook"
SECRET = "tok-s3cret"

BAD_VALUES = [
    f"{SECRET}\n",
    f"{SECRET}\r",
    f"{SECRET}\r\nX-Injected: 1",
    f"{SECRET}\x00",
    f"{SECRET}\x01",
    f"{SECRET}\x7f",
    f" {SECRET}",
    f"{SECRET} ",
    f"{SECRET}é",
]
BAD_NAMES = ["X Token", "X-Token\n", "X:Token", "", "X-Töken", "X-Token\x00"]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _editor(db: AsyncSession) -> dict[str, str]:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="editor", scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


def _create(headers: object) -> dict[str, object]:
    return {
        "provider_type": "webhook",
        "friendly_name": "hook",
        "config": {"post_url": URL, "additional_headers": headers},
    }


# ── at save ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", BAD_VALUES)
async def test_create_refuses_a_bad_value(
    db: AsyncSession, client: AsyncClient, value: str
) -> None:
    resp = await client.post(
        PROVIDERS, json=_create({"X-Token": value}), headers=await _editor(db)
    )
    assert resp.status_code == 422, resp.text
    assert SECRET not in resp.text
    # A NUL is refused by the body's schema (StorableJson), before the
    # provider judges the header.
    assert ("X-Token" in resp.text) != ("\x00" in value), resp.text


@pytest.mark.parametrize("value", [123, None, ["a"], {"a": "b"}, True])
async def test_create_refuses_a_value_that_is_not_text(
    db: AsyncSession, client: AsyncClient, value: object
) -> None:
    resp = await client.post(
        PROVIDERS, json=_create({"X-Token": value}), headers=await _editor(db)
    )
    assert resp.status_code == 422, resp.text
    assert "X-Token" in resp.text


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_create_refuses_a_bad_name(
    db: AsyncSession, client: AsyncClient, name: str
) -> None:
    resp = await client.post(
        PROVIDERS, json=_create({name: "v"}), headers=await _editor(db)
    )
    assert resp.status_code == 422, resp.text
    assert ("header name" in resp.text) != ("\x00" in name), resp.text


async def test_patch_refuses_a_bad_value(db: AsyncSession, client: AsyncClient) -> None:
    provider = make_notification_provider(
        provider_type="webhook", config={"post_url": URL}
    )
    db.add(provider)
    await db.flush()
    resp = await client.patch(
        f"{PROVIDERS}/{provider.id}",
        json={
            "config": {
                "post_url": URL,
                "additional_headers": {"X-Token": f"{SECRET}\n"},
            }
        },
        headers=await _editor(db),
    )
    assert resp.status_code == 422, resp.text
    assert "X-Token" in resp.text
    assert SECRET not in resp.text
    await db.refresh(provider)
    assert "additional_headers" not in provider.config


async def test_clean_headers_are_accepted(
    db: AsyncSession, client: AsyncClient
) -> None:
    headers = {
        "Authorization": f"Bearer {SECRET}",
        "X-Custom_Header.1": "a b\tc ~!#",
        "X-Empty": "",
    }
    resp = await client.post(
        PROVIDERS, json=_create(headers), headers=await _editor(db)
    )
    assert resp.status_code == 201, resp.text


async def test_web_form_refuses_a_bad_value(db: AsyncSession) -> None:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    config = {"post_url": URL, "additional_headers": {"X-Token": f"{SECRET}\n"}}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        resp = await c.post(
            "/notification-providers/create",
            data={
                "provider_type": "webhook",
                "friendly_name": "form hook",
                "config_json": json.dumps(config),
            },
        )
    assert resp.status_code == 422, resp.text
    assert "X-Token" in resp.text
    assert SECRET not in resp.text


# ── a value stored before the refusal ───────────────────────────────────────


@pytest.fixture
def requests_seen(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request the webhook provider sends, answered 200."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)

    def make_client(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(
        hook_mod,
        "httpx",
        types.SimpleNamespace(
            AsyncClient=make_client, HTTPStatusError=httpx.HTTPStatusError
        ),
    )
    return seen


async def _stored_bad(db: AsyncSession):
    provider = make_notification_provider(
        provider_type="webhook",
        config={"post_url": URL, "additional_headers": {"X-Token": f"{SECRET}\n"}},
    )
    db.add(provider)
    await db.flush()
    return provider


async def test_stored_bad_value_fails_before_sending(
    db: AsyncSession, requests_seen: list[httpx.Request]
) -> None:
    provider = await _stored_bad(db)
    alert = make_alert()
    db.add(alert)
    await db.flush()
    ctx = NotificationContext(
        check_name="c", check_type="http", target="t", agent_id=None, status="error"
    )
    rows = await NotificationCoreService.send_notifications(
        db,
        [provider.id],
        ctx,
        alert_id=alert.id,
        check_result_id=None,
        check_result_timestamp=None,
    )
    assert requests_seen == []
    assert [r.status for r in rows] == [NotificationStatus.FAILED]
    error = rows[0].error_message or ""
    assert "X-Token" in error, error
    assert SECRET not in error


async def test_stored_bad_value_test_send_fails_before_sending(
    db: AsyncSession, requests_seen: list[httpx.Request]
) -> None:
    provider = await _stored_bad(db)
    ok, error = await NotificationCoreService.test_provider(db, provider.id)
    assert ok is False
    assert requests_seen == []
    assert "X-Token" in (error or ""), error
    assert SECRET not in (error or "")


async def test_send_checks_headers_before_the_client_opens(
    requests_seen: list[httpx.Request],
) -> None:
    """The provider's own send refuses a header changed after it was built."""
    provider = hook_mod.WebhookNotificationProvider({"post_url": URL})
    provider.config["additional_headers"] = {"X-Token": f"{SECRET}\n"}
    ctx = NotificationContext(
        check_name="c", check_type="http", target="t", agent_id=None, status="error"
    )
    with pytest.raises(ValueError, match="X-Token"):
        await provider.send(ctx)
    assert requests_seen == []
