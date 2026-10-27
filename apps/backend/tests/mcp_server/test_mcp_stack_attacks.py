"""The MCP server's place in the real stack.

No session outlives a request, so an open GET (refused: the server answers POST only)
never holds a transaction or a token's row; through app.main a cross-site browser POST
and a forged Host are refused, and /mcp answers with or without its trailing slash.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from app.core import rate_limit
from app.core.config import settings
from app.mcp.server import MCPAuthMiddleware, mcp_server
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_user
from httpx import ASGITransport
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Mount, Route

pytestmark = pytest.mark.integration


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


async def test_a_get_is_refused_and_holds_no_transaction(
    worker_db: async_sessionmaker[AsyncSession],
) -> None:
    """GET (the server-to-client stream) is 405, and while it is answered no connection
    sits idle in a transaction, so no token row stays locked."""
    async with worker_db() as s:
        user = make_user(role="viewer")
        s.add(user)
        await s.flush()
        _, token = await ApiTokenCoreService.create_token(
            s, user.id, ApiTokenCreate(name="stream", scopes=["*:read"])
        )
        await s.commit()

    rate_limit.credential_attempts.reset()
    rate_limit.mcp_token_calls.reset()
    manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True
    )
    app = Starlette(
        routes=[Mount("/mcp", app=MCPAuthMiddleware(StreamableHTTPASGIApp(manager)))]
    )
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", timeout=10
        ) as client,
    ):
        # Bounded: a server that opens the stream never answers, and the in-process
        # transport enforces no timeout of its own.
        get = await asyncio.wait_for(
            client.get(
                "/mcp/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            ),
            timeout=5,
        )
        post = await asyncio.wait_for(
            client.post("/mcp/", headers=_headers(token), json=_rpc("tools/list")),
            timeout=5,
        )
        async with worker_db() as probe:
            idle_in_tx = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND state = 'idle in transaction'"
                )
            )

    assert get.status_code == 405
    assert post.status_code == 200
    assert idle_in_tx == 0


def _real_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, StreamableHTTPSessionManager]:
    from app.main import app

    manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True
    )
    patched = MCPAuthMiddleware(StreamableHTTPASGIApp(manager))
    found = 0
    for route in app.router.routes:
        if (
            isinstance(route, Mount)
            and route.path == "/mcp"
            or isinstance(route, Route)
            and route.path == "/mcp"
        ):
            monkeypatch.setattr(route, "app", patched)
            found += 1
    assert found == 2, "app.main serves /mcp as a mount and a route"
    return app, manager


async def _token(db: AsyncSession) -> str:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="real", scopes=["*:read"])
    )
    return plaintext


async def test_real_app_controls(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: through app.main, /mcp/ answers; a cross-site browser POST is refused;
    a forged Host is refused."""
    token = await _token(db)
    app, manager = _real_app(monkeypatch)
    host = settings.allowed_hosts[0]
    rate_limit.credential_attempts.reset()
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url=f"http://{host}"
        ) as client,
    ):
        ok = await client.post(
            "/mcp/", headers=_headers(token), json=_rpc("tools/list")
        )
        cross = await client.post(
            "/mcp/",
            headers={**_headers(token), "Origin": "https://evil.example"},
            json=_rpc("tools/list"),
        )
        forged = await client.post(
            "/mcp/",
            headers={**_headers(token), "Host": "evil.example"},
            json=_rpc("tools/list"),
        )
    assert ok.status_code == 200, ok.text
    assert cross.status_code == 403
    assert forged.status_code == 400


async def test_real_app_serves_mcp_without_trailing_slash(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docs and CLAUDE.md say the server is 'at /mcp'; a client configured with
    https://host/mcp gets a 307 to an http:// URL (uvicorn without forwarded proto),
    which drops the Authorization header on most clients."""
    token = await _token(db)
    app, manager = _real_app(monkeypatch)
    host = settings.allowed_hosts[0]
    rate_limit.credential_attempts.reset()
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url=f"https://{host}"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={**_headers(token), "X-Forwarded-Proto": "https"},
            json=_rpc("tools/list"),
        )
    assert response.status_code == 200, (
        response.status_code,
        response.headers.get("location"),
    )
