"""The MCP server against what an attacker or a bad client sends.

Every call is audited, whatever it carries (a NUL, an over-long tool name, no client
address, arguments that do not fit the tool) and an audit that fails never undoes a
tool's change; the gate answers before any schema is disclosed; a call's change is
committed before its answer is sent; only refused tokens count against an address;
free-text filters are bounded as on the admin routes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import pytest
from app.core import rate_limit
from app.mcp.server import MCPAuthMiddleware, mcp_server
from app.models.check_model import Check
from app.models.mcp_tool_call_model import MCPToolCall
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = pytest.mark.integration


def _mcp_app(manager: StreamableHTTPSessionManager) -> Starlette:
    return Starlette(
        routes=[Mount("/mcp", app=MCPAuthMiddleware(StreamableHTTPASGIApp(manager)))]
    )


@asynccontextmanager
async def wire_client(
    wrap: Any = None, client: Any = ("127.0.0.1", 123)
) -> AsyncIterator[httpx.AsyncClient]:
    rate_limit.credential_attempts.reset()
    rate_limit.mcp_token_calls.reset()
    manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True
    )
    app: ASGIApp = _mcp_app(manager)
    if wrap is not None:
        app = wrap(app)
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=ASGITransport(app=app, client=client),
            base_url="http://testserver",
            timeout=30,
        ) as http_client,
    ):
        yield http_client


async def _token(db: AsyncSession, role: str, scopes: list[str]) -> str:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=f"mcp-{role}", scopes=scopes)
    )
    return plaintext


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def _call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return _rpc("tools/call", {"name": tool, "arguments": arguments or {}})


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _result(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert "result" in body, body
    result: dict[str, Any] = body["result"]
    return result


async def _audit_rows(db: AsyncSession, tool: str) -> list[MCPToolCall]:
    db.expire_all()
    return list(
        (
            await db.scalars(select(MCPToolCall).where(MCPToolCall.tool_name == tool))
        ).all()
    )


async def _seed_check(db: AsyncSession, name: str = "orig") -> Check:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="http",
        target="https://app.test/health",
        display_name=name,
    )
    db.add(check)
    await db.flush()
    return check


# ── 1. Audit evasion / silent rollback: a NUL in an ignored argument ──────────


async def test_read_with_nul_extra_argument_leaves_no_audit_row(
    db: AsyncSession,
) -> None:
    """A read succeeds and returns data, but its audit row is never written."""
    token = await _token(db, "viewer", ["*:read"])
    await _seed_check(db)
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call("list_checks", {"note": "\u0000"}),
        )
    result = _result(response)
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"])["total"] >= 1
    rows = await _audit_rows(db, "list_checks")
    assert rows, "a successful read over /mcp left no audit row"


async def test_write_reported_success_is_rolled_back_by_failed_audit(
    db: AsyncSession,
) -> None:
    """update_check answers success with the new name, then the failed audit insert
    rolls the whole session back: the change the agent was told about never lands."""
    token = await _token(db, "editor", ["*"])
    check = await _seed_check(db, "orig")
    check_id = check.id
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "update_check",
                {
                    "check_id": str(check_id),
                    "changes": {"display_name": "renamed"},
                    "why": "\u0000",
                },
            ),
        )
    result = _result(response)
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"])["display_name"] == "renamed"
    db.expire_all()
    stored = await db.get(Check, check_id)
    assert stored is not None
    assert stored.display_name == "renamed", (
        "the agent was told the check was renamed, but the write was rolled back"
    )


async def test_control_update_without_nul_persists(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["*"])
    check = await _seed_check(db, "orig")
    check_id = check.id
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "update_check",
                {
                    "check_id": str(check_id),
                    "changes": {"display_name": "renamed"},
                    "why": "plain",
                },
            ),
        )
    assert _result(response)["isError"] is False
    db.expire_all()
    stored = await db.get(Check, check_id)
    assert stored is not None and stored.display_name == "renamed"
    assert await _audit_rows(db, "update_check")


# ── 2. Audit evasion: a tool name longer than the column ──────────────────────


async def test_long_unknown_tool_name_denial_is_not_audited(db: AsyncSession) -> None:
    token = await _token(db, "viewer", ["*:read"])
    name = "probe_" + "x" * 200
    async with wire_client() as client:
        response = await client.post("/mcp/", headers=_headers(token), json=_call(name))
    result = _result(response)
    assert result["isError"] is True
    assert "FORBIDDEN" in result["content"][0]["text"]
    db.expire_all()
    count = await db.scalar(
        select(func.count())
        .select_from(MCPToolCall)
        .where(MCPToolCall.tool_name.like("probe_%"))
    )
    assert count, "a denied call with a >100-char tool name left no audit row"


# ── 3. Calls refused by the SDK's schema check are never audited ─────────────


async def test_schema_invalid_call_is_not_audited(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["*"])
    async with wire_client() as client:
        response = await client.post(
            "/mcp/", headers=_headers(token), json=_call("create_check", {})
        )
    result = _result(response)
    assert result["isError"] is True
    assert "Field required" in result["content"][0]["text"]
    rows = await _audit_rows(db, "create_check")
    assert rows, "a failed create_check (schema-invalid) left no audit row"


async def test_viewer_schema_invalid_write_is_validated_not_denied(
    db: AsyncSession,
) -> None:
    """A viewer may not call create_check; once an editor has listed tools (the SDK's
    tool cache is process-wide), the viewer's malformed call is answered with the
    tool's schema error instead of FORBIDDEN, and the denial is never audited."""
    editor = await _token(db, "editor", ["*"])
    viewer = await _token(db, "viewer", ["*:read"])
    async with wire_client() as client:
        await client.post("/mcp/", headers=_headers(editor), json=_rpc("tools/list"))
        response = await client.post(
            "/mcp/",
            headers=_headers(viewer),
            json=_call("create_check", {"agent_id": 5}),
        )
    text = _result(response)["content"][0]["text"]
    assert text.startswith("FORBIDDEN"), text
    rows = await _audit_rows(db, "create_check")
    assert any(r.denial_code is not None for r in rows), "denial not audited"


# ── 4. Response is on the wire before the transaction commits ────────────────


async def test_response_is_sent_only_after_commit(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await _token(db, "editor", ["*"])
    check = await _seed_check(db, "orig")
    events: list[str] = []

    real_commit = AsyncSession.commit

    async def recording_commit(self: AsyncSession) -> None:
        events.append("commit")
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)

    def wrap(app: ASGIApp) -> ASGIApp:
        async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
            async def send_wrapper(message: Any) -> None:
                if message["type"] == "http.response.body" and message.get("body"):
                    events.append("body")
                await send(message)

            await app(scope, receive, send_wrapper)

        return recorder

    async with wire_client(wrap) as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "update_check",
                {"check_id": str(check.id), "changes": {"display_name": "renamed"}},
            ),
        )
    assert _result(response)["isError"] is False
    assert "commit" in events and "body" in events, events
    assert events.index("commit") < events.index("body"), (
        f"the tool's success reached the client before its write committed: {events}"
    )


# ── 5. No client address (unix socket, some ASGI servers): every audit fails ──


async def test_no_client_address_rolls_back_writes(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["*"])
    check = await _seed_check(db, "orig")
    check_id = check.id
    async with wire_client(client=None) as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "update_check",
                {"check_id": str(check_id), "changes": {"display_name": "renamed"}},
            ),
        )
    assert _result(response)["isError"] is False
    db.expire_all()
    stored = await db.get(Check, check_id)
    assert stored is not None and stored.display_name == "renamed", (
        "client address 'unknown' failed the INET audit insert and undid the write"
    )


# ── 6. Rate limit: valid tokens count as 'failed' attempts ───────────────────


async def test_valid_tokens_are_not_counted_as_failed_attempts(
    db: AsyncSession,
) -> None:
    tokens = [await _token(db, "viewer", ["*:read"]) for _ in range(11)]
    async with wire_client() as client:
        codes = [
            (
                await client.post("/mcp/", headers=_headers(t), json=_call("whoami"))
            ).status_code
            for t in tokens
        ]
    assert codes == [200] * 11, codes


async def test_control_junk_tokens_are_throttled(db: AsyncSession) -> None:
    async with wire_client() as client:
        codes = [
            (
                await client.post(
                    "/mcp/",
                    headers=_headers("lsw_" + str(i) * 43),
                    json=_call("whoami"),
                )
            ).status_code
            for i in range(11)
        ]
    assert codes[:10] == [401] * 10 and codes[10] == 429, codes


# ── 7. Unbounded search reaches the core (route caps at 255) ─────────────────


async def test_list_checks_search_is_bounded_like_the_route(db: AsyncSession) -> None:
    token = await _token(db, "viewer", ["*:read"])
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call("list_checks", {"search": "a" * 100_000}),
        )
    assert _result(response)["isError"] is True, (
        "a 100k-char search the admin route refuses (max 255) was run"
    )


# keep UUID import used
_ = UUID
