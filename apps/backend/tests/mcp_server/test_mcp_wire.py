"""The MCP surface over real HTTP, end to end (the wire tier ported from LuxWX).

    HTTP → MCPAuthMiddleware → StreamableHTTPSessionManager → call_tool → handler → DB
                                                                         → JSON-RPC body

The assertions are made against the WIRE BODY, not a handler's return value: the
transaction lifecycle, serialisation and error flags live at or above the dispatch
boundary, which direct handler calls cannot reach.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

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
from limits import RateLimitItemPerMinute
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Mount

pytestmark = pytest.mark.integration

SECRET = "S3cret-Pw"


@asynccontextmanager
async def wire_client() -> AsyncIterator[httpx.AsyncClient]:
    """The real MCP ASGI app, mounted exactly as app.main mounts it.

    Entered PER TEST: the session manager's `run()` opens a task group that must be
    exited in the task that entered it, and runs once per instance.
    """
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
            transport=ASGITransport(app=app), base_url="http://testserver"
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


def _payload(response: httpx.Response) -> Any:
    """The tool's JSON payload, unwrapped from the JSON-RPC envelope."""
    result = _result(response)
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


async def _agent(db: AsyncSession) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent.id


async def _last_call(db: AsyncSession, tool: str) -> MCPToolCall | None:
    db.expire_all()
    return (
        await db.scalars(
            select(MCPToolCall)
            .where(MCPToolCall.tool_name == tool)
            .order_by(MCPToolCall.created_at.desc())
        )
    ).first()


# ── Authentication ──────────────────────────────────────────────────────────


async def test_no_token_is_a_bearer_challenge() -> None:
    async with wire_client() as client:
        response = await client.post("/mcp/", json=_rpc("tools/list"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_an_unknown_or_revoked_token_is_refused_alike(db: AsyncSession) -> None:
    token = await _token(db, "admin", ["*"])
    principal = await ApiTokenCoreService.authenticate(db, token)
    assert principal is not None
    await ApiTokenCoreService.revoke_token(
        db, principal.token_id, principal.user.id, principal.user.role
    )
    async with wire_client() as client:
        unknown = await client.post(
            "/mcp/", headers=_headers("lsw_" + "x" * 43), json=_rpc("tools/list")
        )
        revoked = await client.post(
            "/mcp/", headers=_headers(token), json=_rpc("tools/list")
        )
    assert unknown.status_code == revoked.status_code == 401
    assert unknown.json() == revoked.json()
    assert "invalid_token" in revoked.headers["www-authenticate"]


async def test_oauth_discovery_probes_get_404() -> None:
    async with wire_client() as client:
        response = await client.get("/mcp/.well-known/oauth-authorization-server")
    assert response.status_code == 404


async def test_a_token_over_its_call_budget_is_throttled(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await _token(db, "viewer", ["*:read"])
    monkeypatch.setattr(rate_limit.mcp_token_calls, "limit", RateLimitItemPerMinute(2))
    async with wire_client() as client:
        codes = [
            (
                await client.post(
                    "/mcp/", headers=_headers(token), json=_call("whoami")
                )
            ).status_code
            for _ in range(3)
        ]
    assert codes == [200, 200, 429]


# ── What a token sees and may call ──────────────────────────────────────────


async def _tool_names(token: str) -> set[str]:
    async with wire_client() as client:
        response = await client.post(
            "/mcp/", headers=_headers(token), json=_rpc("tools/list")
        )
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in _result(response)["tools"]}


async def test_tools_listed_are_those_the_role_and_scopes_allow(
    db: AsyncSession,
) -> None:
    editor_full = await _tool_names(await _token(db, "editor", ["*"]))
    editor_read = await _tool_names(await _token(db, "editor", ["*:read"]))
    viewer_full = await _tool_names(await _token(db, "viewer", ["*"]))
    checks_only = await _tool_names(await _token(db, "editor", ["checks:read"]))

    assert {"create_check", "create_job", "snooze_alert_check"} <= editor_full
    assert "create_check" not in editor_read
    assert "snooze_alert_check" not in editor_read
    assert "list_checks" in editor_read
    # A viewer's role allows a snooze, never a create.
    assert "snooze_alert_check" in viewer_full
    assert "create_check" not in viewer_full
    assert checks_only == {
        "whoami",
        "list_checks",
        "get_check",
        "get_check_results",
        "get_check_history",
        "get_check_summary",
    }


async def test_whoami_reports_the_token_s_access(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["checks:*"])
    async with wire_client() as client:
        response = await client.post(
            "/mcp/", headers=_headers(token), json=_call("whoami")
        )
    payload = _payload(response)
    assert payload["role"] == "editor"
    assert payload["scopes"] == ["checks:*"]
    assert "create_check" in payload["tools"]
    assert "list_agents" not in payload["tools"]


# ── Calls ───────────────────────────────────────────────────────────────────


async def test_an_editor_creates_a_check_over_the_wire(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["*"])
    agent_id = await _agent(db)
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "create_check",
                {
                    "agent_id": str(agent_id),
                    "display_name": "api-health",
                    "check_type": "http",
                    "target": "https://api.example.test/health",
                },
            ),
        )
    payload = _payload(response)
    assert payload["display_name"] == "api-health"
    db.expire_all()
    assert await db.get(Check, UUID(payload["id"])) is not None
    assert "object at 0x" not in response.text, "a Python repr reached the wire"


@pytest.mark.parametrize(
    ("role", "scopes", "says"),
    [
        ("viewer", ["*"], "editor role"),
        ("editor", ["*:read"], "checks:write"),
    ],
)
async def test_a_call_the_token_may_not_make_is_forbidden_and_audited(
    db: AsyncSession, role: str, scopes: list[str], says: str
) -> None:
    token = await _token(db, role, scopes)
    agent_id = await _agent(db)
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "create_check",
                {
                    "agent_id": str(agent_id),
                    "display_name": "nope",
                    "check_type": "http",
                    "target": "https://nope.test",
                },
            ),
        )
    result = _result(response)
    assert result["isError"] is True
    assert "FORBIDDEN" in result["content"][0]["text"]
    assert says in result["content"][0]["text"]
    db.expire_all()
    made = (await db.scalars(select(Check).where(Check.agent_id == agent_id))).all()
    assert made == []
    row = await _last_call(db, "create_check")
    assert row is not None and row.success is False and row.denial_code is not None


async def test_an_editor_cannot_create_a_synthetic_check(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["*"])
    agent_id = await _agent(db)
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "create_check",
                {
                    "agent_id": str(agent_id),
                    "display_name": "browser",
                    "check_type": "synthetic",
                    "target": "https://app.test",
                    "script_code": "page.goto('https://app.test')",
                },
            ),
        )
    assert _result(response)["isError"] is True
    db.expire_all()
    made = (await db.scalars(select(Check).where(Check.agent_id == agent_id))).all()
    assert made == []
    row = await _last_call(db, "create_check")
    assert row is not None
    assert row.arguments["script_code"] == "<29 characters>"


async def test_a_viewer_reads_a_check_s_credentials_masked(db: AsyncSession) -> None:
    token = await _token(db, "viewer", ["*:read"])
    agent_id = await _agent(db)
    check = make_check(
        agent_id=agent_id,
        check_type="http",
        target=f"https://monitor:{SECRET}@app.test/health",
    )
    db.add(check)
    await db.flush()
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call("get_check", {"check_id": str(check.id)}),
        )
    assert _payload(response)["id"] == str(check.id)
    assert SECRET not in response.text


async def test_the_audit_log_keeps_arguments_with_credentials_masked(
    db: AsyncSession,
) -> None:
    token = await _token(db, "editor", ["*"])
    agent_id = await _agent(db)
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call(
                "create_check",
                {
                    "agent_id": str(agent_id),
                    "display_name": "masked",
                    "check_type": "http",
                    "target": f"https://monitor:{SECRET}@app.test/health",
                },
            ),
        )
    assert _result(response)["isError"] is False
    row = await _last_call(db, "create_check")
    assert row is not None and row.success is True
    assert row.api_token_id is not None and row.user_id is not None
    assert SECRET not in json.dumps(row.arguments)
    assert row.arguments["display_name"] == "masked"


async def test_a_bad_argument_is_an_error_the_agent_can_correct(
    db: AsyncSession,
) -> None:
    token = await _token(db, "viewer", ["*:read"])
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call("get_check", {"check_id": "not-a-uuid"}),
        )
    result = _result(response)
    assert result["isError"] is True
    assert "Invalid UUID for check_id" in result["content"][0]["text"]


async def test_an_unknown_id_is_the_core_s_not_found(db: AsyncSession) -> None:
    token = await _token(db, "viewer", ["*:read"])
    async with wire_client() as client:
        response = await client.post(
            "/mcp/",
            headers=_headers(token),
            json=_call("get_check", {"check_id": str(uuid4())}),
        )
    result = _result(response)
    assert result["isError"] is True
    assert "not found" in result["content"][0]["text"].lower()
