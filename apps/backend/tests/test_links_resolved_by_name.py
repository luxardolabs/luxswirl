"""Links and redirects land on the route they name.

Every web link, form action and redirect is resolved from its route NAME
(`request.app.url_path_for`), not spelled as a path (fw.urls_from_route_names).
The point is that a route that moves takes its links with it, and a name that
no longer exists fails loudly. These tests pin the other half: what renders and
what redirects is exactly the path the named route serves, so a link cannot
silently go dead while every page still renders.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import fields
from uuid import uuid4

import pytest
import pytest_asyncio
from _route_table import iter_routes
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.database import get_db
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from app.services.views.nav_view_service import NavViewService
from fixtures.factories import (
    make_alert,
    make_check_result,
    make_notification_log,
    make_notification_provider,
    make_user,
    setup_agent_with_check,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.routing import compile_path

path_for = app.url_path_for


def _served(path: str) -> bool:
    """True when some route in the app's table serves `path`."""
    return any(
        compile_path(full)[0].match(path) for _, full in iter_routes(app.routes) if full
    )


def test_every_nav_link_is_served_by_a_route() -> None:
    request = Request({"type": "http", "app": app, "headers": [], "path": "/"})
    nav = NavViewService.build_nav_urls(request)

    dead = {
        f.name: getattr(nav, f.name)
        for f in fields(nav)
        if not _served(getattr(nav, f.name))
    }

    assert not dead, f"sidebar/settings links no route serves: {dead}"


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    limiter.reset()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        limiter.reset()


async def _sign_in(
    client: AsyncClient, db: AsyncSession, **user_fields: object
) -> None:
    user = make_user(**user_fields)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)


@pytest.mark.integration
async def test_sidebar_renders_the_resolved_links(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db)

    r = await client.get(path_for("status_page"))

    assert r.status_code == 200
    for name in (
        "status_page",
        "jobs_page",
        "status_pages_list",
        "notification_logs_page",
        "agents_list_page",
        "settings_landing",
        "scheduler_page",
        "database_health_page",
    ):
        assert f'href="{path_for(name)}"' in r.text, name
    assert f'hx-get="{path_for("profile_panel")}"' in r.text
    assert f'action="{path_for("web_logout")}"' in r.text


@pytest.mark.integration
async def test_settings_tabs_render_the_resolved_links(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db)

    r = await client.get(path_for("settings_alerts"))

    assert r.status_code == 200
    for name in (
        "settings_notifications",
        "settings_alerts",
        "settings_registration_keys",
        "settings_defaults",
        "users_page",
        "settings_components",
        "settings_logo_demo",
    ):
        assert f'href="{path_for(name)}"' in r.text, name


@pytest.mark.integration
async def test_sign_in_redirect_names_the_login_page(
    client: AsyncClient, db: AsyncSession
) -> None:
    # A user exists, so this is not first run (which goes to /setup instead).
    db.add(make_user())
    await db.flush()

    r = await client.get(path_for("status_page"))

    assert r.status_code == 307
    dashboard = path_for("status_page").replace("/", "%2F")
    assert r.headers["location"] == f"{path_for('login_page')}?redirect={dashboard}"


@pytest.mark.integration
async def test_a_required_password_change_redirects_to_the_change_page(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db, must_change_password=True)

    r = await client.get(path_for("status_page"))

    assert r.status_code == 307
    assert r.headers["location"] == f"{path_for('change_password_page')}?required=true"
    # ...and the page it sends the user to is not itself redirected.
    page = await client.get(path_for("change_password_page"))
    assert page.status_code == 200
    assert f'action="{path_for("change_password_submit")}"' in page.text


@pytest.mark.integration
async def test_a_viewer_on_an_admin_page_goes_home(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db, role="viewer")

    r = await client.get(path_for("settings_landing"))

    assert r.status_code == 307
    assert r.headers["location"] == f"{path_for('root_redirect')}?error=admin_required"


@pytest.mark.integration
async def test_home_redirects_to_the_dashboard_keeping_the_query(
    client: AsyncClient,
) -> None:
    r = await client.get(path_for("root_redirect"), params={"status": "down"})

    assert r.status_code == 302
    assert r.headers["location"] == f"{path_for('status_page')}?status=down"


@pytest.mark.integration
async def test_login_form_posts_to_the_sign_in_route(
    client: AsyncClient, db: AsyncSession
) -> None:
    # A user exists, so this is not first run (which goes to /setup instead).
    db.add(make_user())
    await db.flush()

    r = await client.get(path_for("login_page"))

    assert r.status_code == 200
    assert f'action="{path_for("login_submit")}"' in r.text


@pytest.mark.integration
async def test_a_failed_password_reset_returns_to_the_users_page(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db)

    r = await client.post(
        path_for("web_reset_user_password", user_id=uuid4()),
        data={"new_password": "AnotherPass123!"},
    )

    assert r.status_code == 302
    assert r.headers["location"].startswith(f"{path_for('users_page')}?error=")


@pytest.mark.integration
async def test_notification_log_row_links_name_their_routes(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _sign_in(client, db)
    agent, check = await setup_agent_with_check(db)
    alert = make_alert()
    provider = make_notification_provider()
    db.add_all([alert, provider])
    await db.flush()
    result = make_check_result(check_id=check.id, agent_id=agent.id)
    db.add(result)
    await db.flush()
    log = make_notification_log(
        alert_id=alert.id,
        notification_provider_id=provider.id,
        check_result_id=result.id,
        check_result_timestamp=result.timestamp,
        check_id=check.id,
    )
    db.add(log)
    await db.flush()

    r = await client.get(path_for("notification_logs_page"))

    assert r.status_code == 200
    assert path_for("check_detail_panel", check_id=check.id) in r.text
    snooze = (
        f"{path_for('snooze_alert_check')}?alert_id={alert.id}"
        f"&amp;check_id={check.id}&amp;log_id={log.id}"
    )
    assert snooze in r.text
