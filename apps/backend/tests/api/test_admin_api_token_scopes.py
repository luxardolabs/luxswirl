"""An API token's scopes narrow its user's role on the admin API.

A read needs `<family>:read`; any other call `<family>:write`, or
`<family>:admin` on a router that is an admin's throughout. Every admin route
but the token's own identity names its family, so a route added later is
gated too. A scope never widens what the role allows.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from _route_table import iter_routes
from app.api.deps import require_scope
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fastapi.routing import APIRoute
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ADMIN = "/api/v1/admin"
# The routes that describe the calling token itself, not any resource.
IDENTITY = {f"{ADMIN}/me", f"{ADMIN}/openapi.json"}
GATE = require_scope("checks").__name__


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _token(db: AsyncSession, role: str, scopes: list[str]) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=scopes)
    )
    return {"Authorization": f"Bearer {plaintext}"}


def _admin_routes() -> list[tuple[APIRoute, str]]:
    """Every admin route but identity, with its full path."""
    routes = [
        (route, path)
        for route, path in iter_routes(app.routes)
        if isinstance(route, APIRoute)
        and path.startswith(ADMIN)
        and path not in IDENTITY
    ]
    # Fewer would mean the table was not walked, and the checks below hold vacuously.
    assert len(routes) > 90
    return routes


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _: str(uuid4()), path)


def test_every_admin_route_but_identity_names_its_scope_family() -> None:
    ungated = [
        f"{sorted(route.methods)} {path}"
        for route, path in _admin_routes()
        if not any(d.call.__name__ == GATE for d in route.dependant.dependencies)
    ]
    assert ungated == []


async def test_a_read_only_admin_token_changes_nothing_anywhere(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "admin", ["*:read"])
    refused = []
    tried = 0
    for route, path in _admin_routes():
        for method in sorted(route.methods - {"GET", "HEAD"}):
            tried += 1
            resp = await client.request(method, _concrete(path), json={}, headers=token)
            if resp.status_code != 403 or "scopes do not grant" not in resp.text:
                refused.append(f"{method} {path}: {resp.status_code}")
    assert tried > 50
    assert refused == []


async def test_a_read_only_token_still_reads(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "admin", ["*:read"])

    for path in ("/checks", "/agents", "/users", "/settings", "/jobs"):
        resp = await client.get(ADMIN + path, headers=token)
        assert resp.status_code == 200, (path, resp.text)


async def test_write_does_not_reach_an_admin_only_family(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "admin", ["*:read", "*:write"])

    users = await client.post(
        f"{ADMIN}/users",
        json={"username": f"u{uuid4().hex[:8]}", "password": "Brand-New-Pass-42"},
        headers=token,
    )
    checks = await client.post(f"{ADMIN}/checks/{uuid4()}/enable", headers=token)

    assert users.status_code == 403, users.text
    assert "users:admin" in users.text
    assert checks.status_code == 404, checks.text


async def test_a_family_scope_reaches_that_family_only(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "admin", ["checks:read"])

    assert (await client.get(f"{ADMIN}/checks", headers=token)).status_code == 200
    resp = await client.get(f"{ADMIN}/agents", headers=token)
    assert resp.status_code == 403
    assert "agents:read" in resp.text


async def test_a_scope_never_widens_the_role(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "viewer", ["*"])

    resp = await client.post(f"{ADMIN}/checks/{uuid4()}/enable", headers=token)

    assert resp.status_code == 403
    assert "Editor or Admin" in resp.text


async def test_me_shows_the_token_s_scopes(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(db, "editor", ["checks:*", "*:read"])

    resp = await client.get(f"{ADMIN}/me", headers=token)

    assert resp.status_code == 200, resp.text
    assert resp.json()["token_scopes"] == ["checks:*", "*:read"]


@pytest.mark.parametrize(
    "scopes", [[], ["checks:delete"], ["unknown:read"], ["CHECKS:READ"]]
)
def test_a_token_names_only_known_scopes(scopes: list[str]) -> None:
    with pytest.raises(ValidationError):
        ApiTokenCreate(name="x", scopes=scopes)


def test_a_token_is_read_only_unless_it_says_otherwise() -> None:
    assert ApiTokenCreate(name="x").scopes == ["*:read"]
