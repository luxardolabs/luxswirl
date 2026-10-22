"""Admin-tier templates render only for admins, and both error cards render.

The admin surface (pages/settings/, partials/settings/) is declared in
.luxarch.toml [target.surfaces]; these requests prove the routes that render
it are admin-gated and that each surface's error partial still renders the
shared error_card, escaped exactly once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from app.web.render import error_partial, settings_error_partial
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

pytestmark = pytest.mark.integration

ADMIN_GETS = [
    "/settings/users",
    "/settings/users/partials/table",
    "/settings/users/create-form",
    "/settings/logo-demo",
    "/scheduler",
    "/database-health",
    "/database-health/refresh",
    "/registration-keys/create-form",
]


async def _client(db: AsyncSession, role: str) -> AsyncIterator[AsyncClient]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


@pytest_asyncio.fixture
async def admin(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async for c in _client(db, "admin"):
        yield c


@pytest_asyncio.fixture(params=["editor", "viewer"])
async def non_admin(request, db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async for c in _client(db, request.param):
        yield c


@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_non_admin_never_gets_an_admin_template(non_admin, path):
    resp = await non_admin.get(path)
    assert resp.status_code == 307, (path, resp.status_code)
    assert "admin_required" in resp.headers["location"]


@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_admin_renders_every_moved_template(admin, path):
    resp = await admin.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:400])


async def test_admin_error_paths_render_the_settings_card(admin):
    for path, code in [
        # scheduler history uses the global session maker (not overridable here)
        (f"/settings/users/{uuid4()}/edit-form", 404),
        (f"/agents/{uuid4()}/import-export-form", 404),
    ]:
        resp = await admin.get(path)
        assert resp.status_code == code, (path, resp.status_code, resp.text[:300])
        assert "data-dismiss-error" in resp.text, path


async def test_tenant_error_path_renders_the_card(admin):
    for path in [
        f"/checks/{uuid4()}/edit-form",
        f"/checks/{uuid4()}/clone-form",
        f"/status-pages/{uuid4()}/edit-form",
    ]:
        resp = await admin.get(path)
        assert resp.status_code == 404, (path, resp.status_code)
        assert "data-dismiss-error" in resp.text, path


def _req() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "app": app,
            "router": app.router,
        }
    )


@pytest.mark.parametrize("fn", [error_partial, settings_error_partial])
def test_error_card_escapes_exactly_once(fn):
    resp = fn(_req(), None, "<script>x</script> & 'q'", status_code=418)
    body = resp.body.decode()
    assert resp.status_code == 418
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt; &amp; " in body
    assert "&amp;lt;" not in body and "&amp;amp;" not in body
