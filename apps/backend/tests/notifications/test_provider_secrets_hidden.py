"""A viewer never reads a notification provider's secrets in the web UI.

The provider forms render the stored config as it is (an SMTP password, a
webhook URL and its headers) so an operator saves without retyping it; they
are an editor's, as saving a provider is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_notification_provider, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "smtp-s3cret"
HOOK = "https://ha.internal/api/webhook/hook-id-s3cret"


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


@pytest_asyncio.fixture
async def setup(db: AsyncSession, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    email = make_notification_provider(
        provider_type="email",
        config={
            "hostname": "smtp.example.test",
            "port": 587,
            "security": "starttls",
            "username": "alerts",
            "password": PASSWORD,
            "from_email": "alerts@example.test",
            "to_email": "ops@example.test",
        },
    )
    hook = make_notification_provider(
        provider_type="webhook",
        config={
            "post_url": HOOK,
            "additional_headers": {"Authorization": "Bearer hdr-s3cret"},
        },
    )
    db.add_all([email, hook])
    await db.flush()
    viewer = await _browser(db, "viewer")
    editor = await _browser(db, "editor")
    yield {"email": email, "hook": hook, "viewer": viewer, "editor": editor}
    await viewer.aclose()
    await editor.aclose()
    limiter.reset()


def _forms(s: dict) -> list[str]:
    return [
        "/notification-providers/create-form",
        f"/notification-providers/{s['email'].id}/edit-form",
        f"/notification-providers/{s['hook'].id}/edit-form",
    ]


async def test_a_viewer_cannot_open_a_provider_form(setup: dict) -> None:
    for path in _forms(setup):
        response = await setup["viewer"].get(path)
        assert response.status_code != 200, path
        for secret in (PASSWORD, "hook-id-s3cret", "hdr-s3cret"):
            assert secret not in response.text, (path, secret)


async def test_an_editor_still_opens_the_provider_forms(setup: dict) -> None:
    for path in _forms(setup):
        assert (await setup["editor"].get(path)).status_code == 200, path
    edit = await setup["editor"].get(
        f"/notification-providers/{setup['email'].id}/edit-form"
    )
    assert PASSWORD in edit.text  # the form edits what is stored
