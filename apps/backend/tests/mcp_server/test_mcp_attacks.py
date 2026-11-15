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
from fixtures.factories import make_agent, make_check, make_check_result, make_user
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


# ── 8. A refusal the admin API names is named over MCP too ───────────────────

_INTERNAL = "the tool failed; the server log has the details"
_SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://a.example')\n"
    "    return {'status': 'success', 'steps': ['Loaded page']}\n"
)


async def _tool(token: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with wire_client() as client:
        response = await client.post(
            "/mcp/", headers=_headers(token), json=_call(tool, arguments)
        )
    return _result(response)


async def _bare_agent(db: AsyncSession) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent.id


async def test_an_ssrf_refused_target_is_named_not_internal(db: AsyncSession) -> None:
    token = await _token(db, "editor", ["*"])
    agent_id = await _bare_agent(db)
    body = {
        "agent_id": str(agent_id),
        "display_name": "meta",
        "check_type": "http",
        "target": "http://169.254.169.254/latest/meta-data/",
    }
    seed = await _seed_check(db)

    results = [
        await _tool(token, "create_check", body),
        await _tool(
            token,
            "update_check",
            {"check_id": str(seed.id), "changes": {"target": body["target"]}},
        ),
        await _tool(
            token,
            "clone_check",
            {"check_id": str(seed.id), "changes": {"target": body["target"]}},
        ),
    ]

    for result in results:
        text = result["content"][0]["text"]
        assert result["isError"] is True, text
        assert _INTERNAL not in text, text
        assert text.startswith("Error: ") and "blocked" in text.lower(), text


async def test_a_refused_synthetic_script_is_named_not_internal(
    db: AsyncSession,
) -> None:
    token = await _token(db, "admin", ["*"])
    agent_id = await _bare_agent(db)

    result = await _tool(
        token,
        "create_check",
        {
            "agent_id": str(agent_id),
            "display_name": "browser-flow",
            "check_type": "synthetic",
            "target": "https://a.example",
            "script_code": "import os\nos.system('id')",
        },
    )

    text = result["content"][0]["text"]
    assert result["isError"] is True, text
    assert _INTERNAL not in text, text
    assert text.startswith("Error: ") and "os" in text, text


async def test_control_an_allowed_synthetic_script_is_made(db: AsyncSession) -> None:
    token = await _token(db, "admin", ["*"])
    agent_id = await _bare_agent(db)

    result = await _tool(
        token,
        "create_check",
        {
            "agent_id": str(agent_id),
            "display_name": "browser-flow",
            "check_type": "synthetic",
            "target": "https://a.example",
            "script_code": _SCRIPT,
        },
    )

    assert result["isError"] is False, result


# ── 9. Argument edges answer a named error, never the internal one ───────────


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_checks", {"tag": "a\x00b"}),
        ("list_checks", {"search": "a\x00b"}),
        ("list_checks", {"tag": "a\x1bb"}),
        ("list_agents", {"search": "a\x00b"}),
    ],
)
async def test_a_control_character_in_a_list_filter_is_named(
    db: AsyncSession, tool: str, arguments: dict[str, Any]
) -> None:
    token = await _token(db, "viewer", ["*:read"])

    result = await _tool(token, tool, arguments)

    text = result["content"][0]["text"]
    assert result["isError"] is True, text
    assert _INTERNAL not in text, text


@pytest.mark.parametrize(
    "name",
    ["web \U0001f469\u200d\U0001f4bb", "می\u200cخواهم", "data\u00adbase"],
    ids=["zwj-emoji", "zwnj-persian", "soft-hyphen"],
)
async def test_a_search_for_a_stored_name_lists(db: AsyncSession, name: str) -> None:
    token = await _token(db, "viewer", ["*:read"])

    result = await _tool(token, "list_checks", {"search": name})

    assert result["isError"] is False, result


async def test_control_a_plain_tag_lists(db: AsyncSession) -> None:
    token = await _token(db, "viewer", ["*:read"])

    result = await _tool(token, "list_checks", {"tag": "edge-01"})

    assert result["isError"] is False, result


@pytest.mark.parametrize(
    "arguments",
    [
        {"until": "0001-01-01T00:00:00"},
        {"since": "0001-01-01T00:00:00", "until": "0001-01-02T00:00:00"},
    ],
)
async def test_results_near_the_first_year_are_a_named_error(
    db: AsyncSession, arguments: dict[str, Any]
) -> None:
    token = await _token(db, "viewer", ["*:read"])
    check = await _seed_check(db)

    result = await _tool(
        token, "get_check_results", {"check_id": str(check.id), **arguments}
    )

    text = result["content"][0]["text"]
    assert result["isError"] is True, text
    assert _INTERNAL not in text, text


# ── 10. A viewer reads a DNS-over-HTTPS nameserver as its origin ─────────────

_DOH_TOKEN = "f00dfacecafe1234"
_DOH = f"https://doh.example.net/{_DOH_TOKEN}"


@pytest.mark.parametrize("tool", ["get_check_results", "get_check_summary"])
async def test_a_viewer_reads_no_doh_token_in_results(
    db: AsyncSession, tool: str
) -> None:
    token = await _token(db, "viewer", ["*:read"])
    agent_id = await _bare_agent(db)
    check = make_check(
        agent_id=agent_id,
        check_type="dns",
        target="example.com",
        check_config={"nameserver": _DOH, "record_type": "A"},
    )
    db.add(check)
    await db.flush()
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent_id,
            metrics=json.dumps({"dns": {"nameserver": _DOH, "port": 443}}),
        )
    )
    await db.flush()

    result = await _tool(token, tool, {"check_id": str(check.id)})

    text = result["content"][0]["text"]
    assert result["isError"] is False, text
    assert _DOH_TOKEN not in text
    assert "https://doh.example.net" in text


# ── 11. A websocket handshake is closed, not crashed on ──────────────────────


async def test_a_websocket_handshake_is_closed_cleanly() -> None:
    async def never(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("a websocket scope reached the transport")

    middleware = MCPAuthMiddleware(never)
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("10.0.0.9", 5555),
        "subprotocols": [],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)

    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]


# keep UUID import used
_ = UUID
