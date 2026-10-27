"""LuxSwirl MCP server — Streamable HTTP transport mounted on the app at /mcp.

Ported from LuxWX's app/mcp/server.py (itself LuxPM's), with these adaptations:

1. Auth validates the user's `lsw_` API token through ApiTokenCoreService, the same
   credential and the same checks as the admin API: unrevoked, unexpired, an active
   user who owes no password change. The token's user, role and scopes are the
   principal; there is no tenant, pack or station pin.
2. Some tools change data, so each tool call runs in a transaction of its own,
   committed BEFORE its result is handed to the transport: an agent told a change was
   made can rely on it, and a call that fails is rolled back whole. LuxWX's tools only
   read, and commit in the middleware after the response.
3. The audit row is written in a transaction of its own, so it can never undo a
   tool's change, nor a tool's failure lose the row.
4. An unexpected error reaches the agent as a fixed message, never its own text: a
   database error's text carries the statement and its parameters.
5. The SDK's own input validation is off: the gate runs first on every call, a
   refused call is answered FORBIDDEN before any schema of the tool is disclosed, and
   every call is audited. The handlers validate their own arguments.

TRANSACTION OWNERSHIP: no session is held across a request. Authentication stamps
the token's `last_used_at` in its own short transaction; `call_tool` owns each call's
(`worker_session`, the app's non-request owner). Tool handlers are CONSUMERS and
never commit. The streamable-HTTP GET (a server-to-client event stream) and DELETE
(ending a session) have no use in this stateless, JSON-only server and are refused.
"""

import time
from typing import Any

from mcp.server import Server
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import rate_limit
from app.core.exceptions import LuxSwirlException
from app.core.request_helpers import client_ip_from_request
from app.db import worker_session
from app.mcp.authorization import AuthorizationError, authorize_tool_call, may_call
from app.mcp.authorization.registry import is_meta_tool
from app.mcp.context import (
    get_origin,
    get_principal,
    set_db,
    set_origin,
    set_principal,
)
from app.mcp.tools import ALL_TOOLS, HANDLERS, ToolError, ToolHandler
from app.mcp.validators import MCPValidationError
from app.models.enum_model import MCPDenialCode
from app.services.core.api_token_core_service import TOKEN_PREFIX, ApiTokenCoreService
from app.services.core.mcp_audit_core_service import MCPAuditCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.mcp.server")

# What an agent reads when a tool fails in a way it cannot correct: the error's own
# text stays in the server log.
_INTERNAL_ERROR = "Error: the tool failed; the server log has the details"

mcp_server = Server("luxswirl")


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    """Show only the tools this token can actually call — role and scope gated.

    Hiding beats returning FORBIDDEN — it keeps the agent's prompt free of operations
    it can never successfully call. Failure is CLOSED: with no auth context, only
    meta tools are listed.
    """
    principal = get_principal()
    if principal is None:
        return [tool for tool in ALL_TOOLS if is_meta_tool(tool.name)]
    return [tool for tool in ALL_TOOLS if may_call(principal, tool.name)]


def _error_result(message: str) -> CallToolResult:
    """A failed tool call: the message the agent reads, plus the flag the client checks.

    One helper so every failure path in `call_tool` carries the flag: the SDK reports a
    returned content list as a success, whatever its text says.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=message)], isError=True
    )


class _ToolFailed(Exception):
    """A tool call's failure, carrying what the agent reads and what the audit keeps."""

    def __init__(self, reply: str, error_message: str) -> None:
        super().__init__(error_message)
        self.reply = reply
        self.error_message = error_message


@mcp_server.call_tool(validate_input=False)
async def call_tool(
    name: str, arguments: dict[str, Any]
) -> list[TextContent] | CallToolResult:
    """Authorize, then dispatch in a transaction of its own, then audit the outcome.

    AUTHORIZATION HAPPENS HERE, once, before any handler runs. Handlers are consumers:
    the core services they call keep their own rules (the synthetic-check rule, the
    viewer's masking), but they do not decide whether the call is permitted.

    The call's transaction is committed before its result is returned, so the SDK
    never sends a success the database did not keep. FAILURES RETURN
    `CallToolResult(isError=True)`, NOT a bare content list, so the audit trail and the
    client agree on what failed.
    """
    start = time.monotonic()
    success = True
    error_message: str | None = None
    denial_code: MCPDenialCode | None = None
    # Map at the write: the SDK dictates this handler's `name` param (protocol
    # contract); mcp tool_name is the column it lands in.
    tool_name = name
    principal = get_principal()

    try:
        if principal is None:
            raise RuntimeError("principal missing from MCP context")
        authorize_tool_call(principal=principal, tool_name=tool_name)
        # Looked up AFTER authorization so an unregistered/unauthorized name is answered
        # by the gate, not by a "no such tool" that would confirm what does exist.
        handler = HANDLERS.get(name)
        if handler is None:
            raise RuntimeError(f"Tool {name} is registered but has no handler")
        return await _run(handler, arguments)
    except AuthorizationError as exc:
        success = False
        error_message = f"{exc.code}: {exc.message}"
        denial_code = exc.code
        logger.info(
            "MCP authorization denied",
            extra={"tool": name, "code": str(exc.code), "required": exc.required_scope},
        )
        return _error_result(f"FORBIDDEN: {exc.message}")
    except _ToolFailed as exc:
        success = False
        error_message = exc.error_message
        return _error_result(exc.reply)
    except Exception as exc:
        success = False
        error_message = type(exc).__name__
        logger.exception("MCP tool error", extra={"tool": name})
        return _error_result(_INTERNAL_ERROR)
    finally:
        await _audit(tool_name, arguments, success, error_message, denial_code, start)


async def _run(handler: ToolHandler, arguments: dict[str, Any]) -> list[TextContent]:
    """Run a handler in a transaction of its own: committed on success, before the
    result leaves; rolled back whole on any failure."""
    try:
        async with worker_session() as db:
            set_db(db)
            return await handler(arguments)
    except (ToolError, MCPValidationError) as exc:
        # Expected, agent-correctable failures (a bad id, an unknown field).
        raise _ToolFailed(f"Error: {exc}", str(exc)) from exc
    except LuxSwirlException as exc:
        # The core's own refusals (not found, invalid, not allowed): written for users.
        raise _ToolFailed(f"Error: {exc.message}", exc.message) from exc


async def _audit(
    tool_name: str,
    arguments: dict[str, Any],
    success: bool,
    error_message: str | None,
    denial_code: MCPDenialCode | None,
    start: float,
) -> None:
    """Record the call, in a transaction of its own. Best-effort: an audit failure is
    logged and never replaces the tool's answer or touches its change."""
    try:
        ip_address, user_agent = get_origin()
        async with worker_session() as audit_db:
            await MCPAuditCoreService.log_call(
                audit_db,
                principal=get_principal(),
                tool_name=tool_name,
                arguments=arguments,
                success=success,
                error_message=error_message,
                duration_ms=int((time.monotonic() - start) * 1000),
                denial_code=denial_code,
                ip_address=ip_address,
                user_agent=user_agent,
            )
    except Exception:
        logger.exception("Failed to log MCP tool call", extra={"tool": tool_name})


# The failed-credential limiter's name for this surface.
_MCP_AUTH_SURFACE = "mcp-auth"
# The per-token call limit's name for this surface.
_MCP_TOKEN_CALLS_SURFACE = "mcp-token-calls"
# The streamable-HTTP methods this stateless, JSON-only server answers.
_METHODS = frozenset({"POST"})


class MCPAuthMiddleware:
    """ASGI middleware authenticating MCP requests by the user's `lsw_` API token.

    Holds no database session across the request: authentication runs in its own
    short transaction, and each tool call opens its own in `call_tool`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # 404 the OAuth discovery/registration probes that reach the /mcp mount. MCP
        # clients probe these before falling back to header auth — a 401 here would
        # kick them into an OAuth flow we don't implement. Ported verbatim.
        path = request.url.path
        if "/.well-known/" in path or path.rstrip("/").endswith("/register"):
            response = JSONResponse({"error": "not_found"}, status_code=404)
            await response(scope, receive, send)
            return

        if request.method not in _METHODS:
            response = JSONResponse(
                {"error": "Method not allowed: this server answers POST only"},
                status_code=405,
                headers={"Allow": "POST"},
            )
            await response(scope, receive, send)
            return

        token = self._extract_token(request)
        if not token:
            # RFC 6750 §3: a bearer 401 carries a WWW-Authenticate challenge.
            response = JSONResponse(
                {"error": "API token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="luxswirl-mcp"'},
            )
            await response(scope, receive, send)
            return

        # A client that has presented too many refused tokens this minute is refused
        # before its token is looked up; only a refused token counts against it, so
        # valid tokens from a shared address never do (FLEET-RATE-LIMIT-STANDARD §3).
        client_ip = client_ip_from_request(request)
        retry_after = rate_limit.credential_attempts.over_limit(
            _MCP_AUTH_SURFACE, client_ip
        )
        if retry_after is not None:
            response = JSONResponse(
                {"error": "Too many authentication attempts"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # Its own short transaction: the last_used_at stamp is committed now, so no
        # row lock on the token outlives authentication.
        async with worker_session() as db:
            principal = await ApiTokenCoreService.authenticate(db, token)
        if principal is None:
            rate_limit.credential_attempts.count(_MCP_AUTH_SURFACE, client_ip)
            # Deliberately indistinguishable across unknown/revoked/expired/inactive
            # user — don't help an attacker enumerate valid tokens.
            response = JSONResponse(
                {"error": "Invalid or revoked API token"},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="luxswirl-mcp", error="invalid_token", '
                        'error_description="The API token is invalid, revoked '
                        'or expired"'
                    )
                },
            )
            await response(scope, receive, send)
            return

        # The token's own call budget: caps what a leaked token can pull.
        retry_after = rate_limit.mcp_token_calls.retry_after(
            _MCP_TOKEN_CALLS_SURFACE, str(principal.token_id)
        )
        if retry_after is not None:
            logger.info(
                "MCP request refused: token over its call limit",
                extra={"token_id": str(principal.token_id)},
            )
            response = JSONResponse(
                {"error": "Too many requests for this API token"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        set_principal(principal)
        # The proxy-aware resolver the rate limiter uses: X-Forwarded-For is only
        # honored when the peer is a trusted proxy, so a caller cannot forge it.
        set_origin(client_ip, request.headers.get("user-agent"))
        await self.app(scope, receive, send)

    @staticmethod
    def _extract_token(request: Request) -> str | None:
        """Pull the token from Authorization: Bearer or X-API-Key.

        Both are accepted because MCP clients differ in which they send. Only tokens
        carrying our prefix are returned, so a session cookie value can never be
        mistaken for one.
        """
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip().startswith(TOKEN_PREFIX):
            return value.strip()

        api_key = request.headers.get("x-api-key", "").strip()
        if api_key.startswith(TOKEN_PREFIX):
            return api_key

        return None


# Started/stopped by the app lifespan (app/main.py).
mcp_session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    json_response=True,
    stateless=True,
)


def create_mcp_app() -> ASGIApp:
    """Create the MCP Streamable HTTP ASGI app wrapped in auth middleware."""
    return MCPAuthMiddleware(StreamableHTTPASGIApp(mcp_session_manager))
