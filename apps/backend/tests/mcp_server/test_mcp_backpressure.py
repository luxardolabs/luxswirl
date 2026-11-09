"""One caller cannot queue MCP calls without limit, and a stop keeps a call's audit row.

The audit masks a call's arguments on one thread before the answer leaves, so a
viewer sending many large calls once grew the server by gigabytes and made every
client wait minutes. Now a body over `MCP_MAX_BODY` is refused (413) before the
transport parses it, whatever length it declares, and a token has at most
`MCP_TOKEN_IN_FLIGHT` requests in flight (429 past it).

A call cancelled while its audit row waited for the masking thread (the server
stopping) lost the row, though its change was committed. It now writes the row
anyway, shielded, with the arguments counted, not kept.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.core import rate_limit
from app.db.database import AsyncSessionLocal
from app.mcp import server as mcp
from app.mcp.server import MCPAuthMiddleware, mcp_server
from app.models.check_model import Check
from app.models.mcp_tool_call_model import MCPToolCall
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core import mcp_audit_core_service as audit
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import func, select
from starlette.applications import Starlette
from starlette.routing import Mount

pytestmark = pytest.mark.integration

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _wire() -> tuple[StreamableHTTPSessionManager, Starlette]:
    rate_limit.credential_attempts.reset()
    rate_limit.mcp_token_calls.reset()
    manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True
    )
    app = Starlette(
        routes=[Mount("/mcp", app=MCPAuthMiddleware(StreamableHTTPASGIApp(manager)))]
    )
    return manager, app


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    manager, app = _wire()
    async with (
        manager.run(),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        yield c


async def _token(role: str) -> tuple[str, Any]:
    """A committed token for a new user of ``role``, and a committed agent's id."""
    async with AsyncSessionLocal() as s:
        user = make_user(role=role)
        agent = make_agent()
        s.add_all([user, agent])
        await s.flush()
        _, plain = await ApiTokenCoreService.create_token(
            s, user.id, ApiTokenCreate(name=f"bp-{uuid4().hex[:6]}", scopes=["*"])
        )
        agent_id = agent.id
        await s.commit()
    return plain, agent_id


def _create(agent_id: Any, name: str, padding: int = 0) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "agent_id": str(agent_id),
        "display_name": name,
        "check_type": "http",
        "target": "https://93.184.216.34/",
    }
    if padding:
        arguments["description"] = "a" * padding
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_check", "arguments": arguments},
    }


async def _rows(tool: str) -> int:
    async with AsyncSessionLocal() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(MCPToolCall)
            .where(MCPToolCall.tool_name == tool)
        )
    return count or 0


@asynccontextmanager
async def _masking_held() -> AsyncIterator[threading.Event]:
    """Hold the audit's one masking thread busy until the event is set."""
    release = threading.Event()
    audit._MASKING.submit(release.wait, 60)
    try:
        yield release
    finally:
        release.set()


async def test_a_body_over_the_limit_is_refused_before_any_tool_runs(
    worker_db: Any,
) -> None:
    token, agent_id = await _token("editor")
    name = f"big-{uuid4().hex[:8]}"
    body = json.dumps(_create(agent_id, name, padding=mcp.MCP_MAX_BODY)).encode()

    async with _client() as c:
        r = await c.post(
            "/mcp/",
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            content=body,
        )

    assert r.status_code == 413
    assert await _rows("create_check") == 0


async def test_a_body_that_declares_no_length_is_cut_at_the_limit(
    worker_db: Any,
) -> None:
    """Chunked, with no Content-Length: refused by what arrives, not by what it says."""
    token, agent_id = await _token("editor")
    body = json.dumps(_create(agent_id, "chunked", padding=mcp.MCP_MAX_BODY)).encode()

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(body), 64 * 1024):
            yield body[i : i + 64 * 1024]

    async with _client() as c:
        r = await c.post(
            "/mcp/",
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            content=chunks(),
        )

    assert r.status_code == 413
    assert await _rows("create_check") == 0


async def test_a_body_under_the_limit_still_reaches_its_tool(worker_db: Any) -> None:
    token, agent_id = await _token("editor")
    name = f"ok-{uuid4().hex[:8]}"

    async with _client() as c:
        r = await c.post(
            "/mcp/",
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            json=_create(agent_id, name, padding=500),
        )

    assert r.status_code == 200
    assert r.json()["result"]["isError"] is False, r.json()


async def test_one_token_has_at_most_its_limit_in_flight(worker_db: Any) -> None:
    """Calls past the limit, while the others wait behind the audit, are 429; another
    token is not held up by them, and the token is free again once they finish."""
    token, agent_id = await _token("viewer")
    other, _ = await _token("viewer")
    auth = {**HEADERS, "Authorization": f"Bearer {token}"}

    async with _client() as c, _masking_held() as release:
        held = [
            asyncio.create_task(
                c.post("/mcp/", headers=auth, json=_create(agent_id, f"h{i}"))
            )
            for i in range(mcp.MCP_TOKEN_IN_FLIGHT)
        ]
        for _ in range(200):
            if sum(mcp._in_flight.values()) >= mcp.MCP_TOKEN_IN_FLIGHT:
                break
            await asyncio.sleep(0.02)
        over = await c.post("/mcp/", headers=auth, json=_create(agent_id, "over"))
        others = await c.post(
            "/mcp/",
            headers={**HEADERS, "Authorization": f"Bearer {other}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        release.set()
        answers = await asyncio.gather(*held)
        after = await c.post("/mcp/", headers=auth, json=_create(agent_id, "after"))

    assert over.status_code == 429
    assert over.headers["Retry-After"] == "1"
    assert others.status_code == 200
    assert [a.status_code for a in answers] == [200] * mcp.MCP_TOKEN_IN_FLIGHT
    assert after.status_code == 200
    assert mcp._in_flight == {}


async def test_a_stop_keeps_the_audit_row_of_a_committed_call(worker_db: Any) -> None:
    token, agent_id = await _token("editor")
    name = f"stop-{uuid4().hex[:8]}"
    secret_target = "https://u:hunter2@93.184.216.34/"
    body = _create(agent_id, name)
    body["params"]["arguments"]["target"] = secret_target
    before = await _rows("create_check")
    manager, app = _wire()

    async with (
        _masking_held() as release,
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        async with manager.run():
            post = asyncio.create_task(
                c.post(
                    "/mcp/",
                    headers={**HEADERS, "Authorization": f"Bearer {token}"},
                    json=body,
                )
            )
            for _ in range(200):
                if post.done() or audit._MASKING._work_queue.qsize() >= 1:
                    break
                await asyncio.sleep(0.05)
            # leaving manager.run() cancels its task group: the server stopping
        post.cancel()
        await asyncio.gather(post, return_exceptions=True)
        release.set()

    async with AsyncSessionLocal() as s:
        made = await s.scalar(
            select(func.count()).select_from(Check).where(Check.display_name == name)
        )
        row = (
            await s.scalars(
                select(MCPToolCall)
                .where(MCPToolCall.tool_name == "create_check")
                .order_by(MCPToolCall.created_at.desc())
                .limit(1)
            )
        ).first()
    assert made == 1
    assert await _rows("create_check") == before + 1
    assert row is not None and row.success is True
    assert row.arguments == {
        audit.INTERRUPTED_KEY: (
            "4 argument(s), not kept: the server stopped before they were masked"
        )
    }
    assert "hunter2" not in json.dumps(row.arguments)
