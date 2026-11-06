"""Unlocking a user from the edit panel refreshes the page through HX-Trigger.

The panel's form posted with hx-swap="none" and then ran
`window.location.reload()` itself; the route answered with a redirect HTMX
followed and threw away. The route now answers with the toast and the
closeSidePanel/refreshPage events app.js listens for.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from html.parser import HTMLParser
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def test_unlock_refreshes_the_page(client: AsyncClient, db: AsyncSession) -> None:
    locked = make_user(
        role="viewer",
        failed_login_attempts=5,
        locked_until=utc_now() + timedelta(hours=1),
    )
    db.add(locked)
    await db.flush()

    resp = await client.post(f"/settings/users/{locked.id}/unlock")

    assert resp.status_code == 200, resp.text
    events = json.loads(resp.headers["HX-Trigger"])
    assert {"closeSidePanel", "refreshPage", "showToast"} <= set(events)
    assert locked.username in json.dumps(events["showToast"])


async def test_unlocking_an_unknown_user_toasts_the_error(client: AsyncClient) -> None:
    resp = await client.post(f"/settings/users/{uuid4()}/unlock")

    assert resp.status_code != 302
    assert "refreshPage" not in resp.headers.get("HX-Trigger", "")


class _UnlockButtons(HTMLParser):
    """Each Unlock Account button's attributes as written, and whether a form holds it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms_open = 0
        self.buttons: list[tuple[list[tuple[str, str | None]], bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.forms_open += 1
        elif tag == "button" and ("title", "Unlock Account") in attrs:
            self.buttons.append((attrs, self.forms_open > 0))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.forms_open -= 1


@pytest.mark.parametrize("path", ["/settings/users", "/settings/users/partials/table"])
async def test_the_list_unlock_button_posts_with_htmx(
    client: AsyncClient, db: AsyncSession, path: str
) -> None:
    """The route answers an empty body with HX-Trigger, so only HTMX can show the result."""
    locked = make_user(
        role="viewer",
        failed_login_attempts=5,
        locked_until=utc_now() + timedelta(hours=1),
    )
    db.add(locked)
    await db.flush()

    resp = await client.get(path)

    assert resp.status_code == 200, resp.text
    parser = _UnlockButtons()
    parser.feed(resp.text)
    assert len(parser.buttons) == 1, resp.text
    attrs, in_a_form = parser.buttons[0]
    names = [name for name, _ in attrs]
    assert len(names) == len(set(names)), names
    written = dict(attrs)
    assert written["hx-post"] == f"/settings/users/{locked.id}/unlock"
    assert written["hx-swap"] == "none"
    assert written["type"] == "button"
    assert not in_a_form
