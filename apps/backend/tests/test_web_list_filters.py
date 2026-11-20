"""The web lists' text filters are held as the admin API's are.

Every web page that filters a list by typed text (agents, users, notification
logs, the dashboard, a check's dependents, a status page's available checks)
refuses a control character, an over-long value and, for the users list, a role
that is not one, with a 422 at the edge. A NUL reaching PostgreSQL is a 500, and
an unknown role reaching the enum column is too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.main import app
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def admin_web(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


@pytest_asyncio.fixture
async def paths(db: AsyncSession) -> dict[str, str]:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    page = StatusPage(name="page", slug=f"p-{uuid4().hex[:8]}", items=[])
    db.add_all([check, page])
    await db.flush()
    return {
        "agents": "/agents",
        "users": "/settings/users",
        "users_table": "/settings/users/partials/table",
        "notification_logs": "/notification-logs",
        "dashboard": "/dashboard",
        "dependents": f"/checks/{check.id}/dependents",
        "available": f"/status-pages/{page.id}/available-checks",
    }


TEXT_FILTERS = [
    ("agents", "search"),
    ("users", "search"),
    ("users_table", "search"),
    ("notification_logs", "search"),
    ("dashboard", "search"),
    ("dashboard", "tags"),
    ("dependents", "search"),
    ("dependents", "tags"),
    ("dependents", "agent_name"),
    ("available", "search"),
    ("available", "tags"),
]


@pytest.mark.parametrize(("page", "param"), TEXT_FILTERS)
async def test_a_nul_in_a_text_filter_is_refused(
    admin_web: AsyncClient, paths: dict[str, str], page: str, param: str
) -> None:
    resp = await admin_web.get(paths[page], params={param: "a\x00b"})
    assert resp.status_code == 422, (resp.status_code, resp.text[:300])


@pytest.mark.parametrize(("page", "param"), TEXT_FILTERS)
async def test_an_over_long_text_filter_is_refused(
    admin_web: AsyncClient, paths: dict[str, str], page: str, param: str
) -> None:
    resp = await admin_web.get(paths[page], params={param: "x" * 20_000})
    assert resp.status_code == 422, (resp.status_code, resp.text[:300])


@pytest.mark.parametrize(("page", "param"), TEXT_FILTERS)
async def test_an_ordinary_or_blank_text_filter_is_answered(
    admin_web: AsyncClient, paths: dict[str, str], page: str, param: str
) -> None:
    for value in ("web", ""):
        resp = await admin_web.get(paths[page], params={param: value})
        assert resp.status_code == 200, (value, resp.status_code, resp.text[:300])


@pytest.mark.parametrize("page", ["users", "users_table"])
async def test_the_users_list_refuses_a_role_that_is_not_one(
    admin_web: AsyncClient, paths: dict[str, str], page: str
) -> None:
    resp = await admin_web.get(paths[page], params={"role": "superuser"})
    assert resp.status_code == 422, (resp.status_code, resp.text[:300])


@pytest.mark.parametrize("page", ["users", "users_table"])
async def test_the_users_list_filters_by_a_real_role_or_none(
    admin_web: AsyncClient, paths: dict[str, str], page: str
) -> None:
    for role in ("viewer", ""):
        resp = await admin_web.get(paths[page], params={"role": role})
        assert resp.status_code == 200, (role, resp.status_code, resp.text[:300])
