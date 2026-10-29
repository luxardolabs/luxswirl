"""No cache keeps a dynamic response: each answers `Cache-Control: no-store`.

Several responses carry a secret shown once: a new agent key, a recovered key,
a registration key, an API token, the metrics token, an agent's recovery
secret. A shared proxy or the browser cache must not keep any of them, so every
dynamic response the app answers is marked, not only the routes known today to
carry one.
Static assets keep their own caching, and a route that sets `Cache-Control`
itself keeps its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security_headers import add_security_headers
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fastapi import Response
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient, Headers
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


def _not_stored(headers: Headers) -> None:
    assert headers.get("cache-control") == "no-store"
    assert headers.get("pragma") == "no-cache"


async def test_a_new_registration_key_is_not_stored_by_any_cache(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await ApiTokenCoreService.create_token(
        db, admin.id, ApiTokenCreate(name="admin", scopes=["*"])
    )

    response = await client.post(
        "/api/v1/admin/registration-keys",
        json={"name": "cache"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["key"]
    _not_stored(response.headers)


@pytest.mark.parametrize("path", ["/health", "/login", "/api/v1/admin/me"])
async def test_every_dynamic_response_is_marked(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    _not_stored(response.headers)


async def test_a_refusal_is_marked_too(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/agent/register",
        json={"hostname": "probe"},
        headers={"Authorization": "Bearer not-a-key"},
    )

    assert response.status_code == 401
    _not_stored(response.headers)


async def test_a_static_asset_keeps_its_own_caching(client: AsyncClient) -> None:
    response = await client.get("/static/js/agents.js")

    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")
    assert "pragma" not in response.headers


async def test_an_unhandled_error_s_500_is_marked_and_keeps_the_policy() -> None:
    """Starlette answers an unhandled exception outside every user middleware:
    the app's handler marks that 500 itself."""

    async def _boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/__boom", _boom, methods=["GET"])
    route = app.router.routes[-1]
    # The 500 as sent to the client: ServerErrorMiddleware sends the handler's
    # answer, then re-raises, which a client library would turn into a bare 500.
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/__boom",
        "raw_path": b"/__boom",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", settings.allowed_hosts[0].encode())],
        "client": ("127.0.0.1", 1),
        "server": (settings.allowed_hosts[0], 443),
    }
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await app(scope, receive, send)
    finally:
        app.router.routes.remove(route)

    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    headers = Headers([(k.decode(), v.decode()) for k, v in start["headers"]])
    assert start["status"] == 500
    _not_stored(headers)
    assert "script-src" in headers["content-security-policy"]


def test_a_response_that_sets_its_own_cache_control_keeps_it() -> None:
    response = Response(headers={"Cache-Control": "private, max-age=60"})

    add_security_headers("/api/v1/admin/me", response)

    assert response.headers["cache-control"] == "private, max-age=60"
    assert "pragma" not in response.headers


@pytest.mark.parametrize("path", ["/staticfoo", "/static", "/api/static/x"])
def test_only_the_static_mount_s_assets_go_unmarked(path: str) -> None:
    response = add_security_headers(path, Response())

    _not_stored(response.headers)
