"""A viewer never reads a check's credentials in the web UI.

The forms that render a check's stored `connection_string` and `script_code`
(create, edit, clone, and the assignment selector they load) are an editor's,
as saving them is. Every page that displays a check's target, which anyone
signed in may load, shows it with its password masked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.credential_mask import mask_credentials
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "s3cret-pw"
DSN = f"postgresql://monitor:{PASSWORD}@db.internal:5432/app"


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
    agent = make_agent()
    db.add(agent)
    await db.flush()
    pg = make_check(
        agent_id=agent.id, check_type="postgres", target=DSN, connection_string=DSN
    )
    script = make_check(
        agent_id=agent.id,
        check_type="synthetic",
        target="https://a.example",
        script_code="secret_script()",
    )
    db.add_all([pg, script])
    await db.flush()
    viewer = await _browser(db, "viewer")
    editor = await _browser(db, "editor")
    yield {
        "agent": agent,
        "pg": pg,
        "script": script,
        "viewer": viewer,
        "editor": editor,
    }
    await viewer.aclose()
    await editor.aclose()
    limiter.reset()


def _forms(s: dict) -> list[str]:
    return [
        "/checks/create-form",
        "/checks/assignment-mode-selector",
        f"/checks/{s['pg'].id}/edit-form",
        f"/checks/{s['pg'].id}/clone-form",
        f"/checks/{s['script'].id}/edit-form",
    ]


async def test_a_viewer_cannot_open_a_check_form(setup: dict) -> None:
    for path in _forms(setup):
        response = await setup["viewer"].get(path)
        assert response.status_code != 200, path
        assert PASSWORD not in response.text and "secret_script" not in response.text


async def test_an_editor_still_opens_the_check_forms(setup: dict) -> None:
    for path in _forms(setup):
        assert (await setup["editor"].get(path)).status_code == 200, path
    edit = await setup["editor"].get(f"/checks/{setup['pg'].id}/edit-form")
    assert PASSWORD in edit.text  # the form edits what is stored


async def test_the_check_detail_panel_masks_the_target(setup: dict) -> None:
    response = await setup["viewer"].get(f"/check/{setup['pg'].id}")
    assert response.status_code == 200
    assert PASSWORD not in response.text
    assert "postgresql://***@db.internal:5432/app" in response.text


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (DSN, "postgresql://***@db.internal:5432/app"),
        ("https://svc:pw@api.example/h", "https://***@api.example/h"),
        ("postgresql://:pw@db/app", "postgresql://***@db/app"),
        ("postgresql://u:p w@db/app", "postgresql://***@db/app"),
        (
            "host=db user=u password='p w' dbname=app",
            "host=db user=u password=*** dbname=app",
        ),
        ("Server=db;Uid=u;Pwd=p;", "Server=db;Uid=u;Pwd=***;"),
        ("https://example.com/health", "https://example.com/health"),
        ("10.0.0.5", "10.0.0.5"),
        (None, None),
    ],
)
def test_mask_credentials(value: str | None, shown: str | None) -> None:
    assert mask_credentials(value) == shown
