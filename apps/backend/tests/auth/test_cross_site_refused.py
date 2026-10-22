"""A browser POST from another site is refused before any route runs (login CSRF).

SameSite=Lax keeps the victim's cookie off a cross-site POST, but not the response's
Set-Cookie: a hostile page that auto-submits the login form signs the visitor into
the attacker's account. These requests go through the real app, so a guard that was
not installed fails here. FLEET-AUTH-SESSION-STANDARD §5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import ServerSettings, settings
from app.core.rate_limit import limiter
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

OWN = settings.allowed_hosts[0]
AUTH_POSTS = ["/login", "/logout"]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=f"https://{OWN}")


@pytest_asyncio.fixture
async def routed(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A client whose requests reach a route, on the test database."""

    # Each test logs in for real: start from an empty login budget, and leave one.
    limiter.reset()
    try:
        async with _client() as c:
            yield c
    finally:
        limiter.reset()


@pytest.mark.parametrize("path", AUTH_POSTS)
async def test_cross_site_fetch_metadata_is_refused(path: str) -> None:
    async with _client() as c:
        r = await c.post(path, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("origin", ["https://attacker.example", "null"])
async def test_foreign_or_null_origin_is_refused(origin: str) -> None:
    async with _client() as c:
        r = await c.post("/login", headers={"Origin": origin})
    assert r.status_code == 403


async def test_same_origin_login_reaches_the_route(routed: AsyncClient) -> None:
    r = await routed.post(
        "/login",
        headers={"Origin": f"https://{OWN}", "Sec-Fetch-Site": "same-origin"},
        data={"username": "nobody", "password": "wrong"},
    )
    assert r.status_code not in (403, 500)


async def test_a_request_without_browser_headers_passes(routed: AsyncClient) -> None:
    # The agent and webhooks send neither Origin nor Sec-Fetch-Site: the request
    # reaches the route, which refuses the unknown registration key itself.
    r = await routed.post(
        "/api/v1/agents/register",
        json={"hostname": "x"},
        headers={"Authorization": "Bearer not-a-registration-key"},
    )
    assert r.status_code == 401


def test_cors_origin_hosts_are_the_origins_hostnames() -> None:
    server = ServerSettings(
        cors_origins=["https://ui.example.com:9000", "https://API.example.com", "*", ""]
    )
    assert server.cors_origin_hosts == ["ui.example.com", "api.example.com"]
