"""LuxSwirl MCP server — Streamable HTTP transport mounted on the app at /mcp.

Ported from LuxWX's app/mcp/server.py (itself LuxPM's), with these adaptations:

1. Auth validates the user's `lsw_` API token through ApiTokenCoreService, the same
   credential and the same checks as the admin API: unrevoked, unexpired, an active
   user who owes no password change. The token's user, role and scopes are the
   principal; there is no tenant, pack or station pin.
2. Some tools change data, so a call that fails is rolled back before its audit row
   is written: a half-made change is never committed. LuxWX's tools only read.
3. An unexpected error reaches the agent as a fixed message, never its own text: a
   database error's text carries the statement and its parameters.

TRANSACTION OWNERSHIP: this middleware owns the request's session (`worker_session`,
the app's non-request owner), committing on success and rolling back on failure. Tool
handlers are request-scoped CONSUMERS and never commit.
"""

import hashlib
import time
from typing import Any

from mcp.server import Server
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from sqlalchemy.ext.asyncio import AsyncSession
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
    get_db,
    get_origin,
    get_principal,
    set_origin,
    set_request_context,
)
from app.mcp.tools import ALL_TOOLS, HANDLERS, ToolError
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


@mcp_server.call_tool()
async def call_tool(
    name: str, arguments: dict[str, Any]
) -> list[TextContent] | CallToolResult:
    """Authorize, then dispatch, then audit whatever the outcome.

    AUTHORIZATION HAPPENS HERE, once, before any handler runs. Handlers are consumers:
    the core services they call keep their own rules (the synthetic-check rule, the
    viewer's masking), but they do not decide whether the call is permitted.

    FAILURES RETURN `CallToolResult(isError=True)`, NOT a bare content list, so the
    audit trail and the client agree on what failed.
    """
    start = time.monotonic()
    success = True
    error_message: str | None = None
    denial_code: MCPDenialCode | None = None
    # Map at the write: the SDK dictates this handler's `name` param (protocol
    # contract); mcp tool_name is the column it lands in.
    tool_name = name

    try:
        principal = get_principal()
        if principal is None:
            raise RuntimeError("principal missing from MCP context")

        authorize_tool_call(principal=principal, tool_name=tool_name)

        # Looked up AFTER authorization so an unregistered/unauthorized name is answered
        # by the gate, not by a "no such tool" that would confirm what does exist.
        handler = HANDLERS.get(name)
        if handler is None:
            raise RuntimeError(f"Tool {name} is registered but has no handler")

        return await handler(arguments)
    except AuthorizationError as exc:
        success = False
        error_message = f"{exc.code}: {exc.message}"
        denial_code = exc.code
        logger.info(
            "MCP authorization denied",
            extra={"tool": name, "code": str(exc.code), "required": exc.required_scope},
        )
        return _error_result(f"FORBIDDEN: {exc.message}")
    except (ToolError, MCPValidationError) as exc:
        # Expected, agent-correctable failures (a bad id, an unknown field).
        success = False
        error_message = str(exc)
        await _roll_back()
        return _error_result(f"Error: {exc}")
    except LuxSwirlException as exc:
        # The core's own refusals (not found, invalid, not allowed): written for users.
        success = False
        error_message = exc.message
        await _roll_back()
        return _error_result(f"Error: {exc.message}")
    except Exception as exc:
        success = False
        error_message = type(exc).__name__
        logger.exception("MCP tool error", extra={"tool": name})
        await _roll_back()
        return _error_result(_INTERNAL_ERROR)
    finally:
        await _audit(tool_name, arguments, success, error_message, denial_code, start)


async def _roll_back() -> None:
    """Undo whatever a failed tool changed, so the middleware commits only the audit."""
    try:
        await get_db().rollback()
    except Exception:
        logger.exception("Rollback after a failed MCP tool call failed")


async def _audit(
    tool_name: str,
    arguments: dict[str, Any],
    success: bool,
    error_message: str | None,
    denial_code: MCPDenialCode | None,
    start: float,
) -> None:
    """Record the call. Best-effort: an audit failure never replaces the tool's answer."""
    audit_db: AsyncSession | None = None
    try:
        audit_db = get_db()
        principal = get_principal()
        ip_address, user_agent = get_origin()
        await MCPAuditCoreService.log_call(
            audit_db,
            principal=principal,
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
        # Audit must never poison the outer session. If the insert left the session
        # pending-rollback, roll back here so the middleware's commit can still
        # proceed and the client gets the tool's real response.
        logger.warning(
            "Failed to log MCP tool call", extra={"tool": tool_name}, exc_info=True
        )
        if audit_db is not None:
            try:
                await audit_db.rollback()
            except Exception:
                logger.exception(
                    "Rollback after failed MCP tool-call audit failed",
                    extra={"tool": tool_name},
                )


# The failed-credential limiter's name for this surface.
_MCP_AUTH_SURFACE = "mcp-auth"
# The per-token call limit's name for this surface.
_MCP_TOKEN_CALLS_SURFACE = "mcp-token-calls"
# How long a token proven valid skips the failed-credential count.
_PROVEN_SECONDS = 60.0
_recently_proven: dict[str, float] = {}


def _digest(token: str) -> str:
    return hashlib.sha256(b"mcp-proven:" + token.encode("utf-8")).hexdigest()


def _is_recently_proven(token: str) -> bool:
    until = _recently_proven.get(_digest(token))
    return until is not None and until > time.monotonic()


def _mark_proven(token: str) -> None:
    now = time.monotonic()
    for digest, until in list(_recently_proven.items()):
        if until <= now:
            del _recently_proven[digest]
    _recently_proven[_digest(token)] = now + _PROVEN_SECONDS


class MCPAuthMiddleware:
    """ASGI middleware authenticating MCP requests by the user's `lsw_` API token.

    Owns the request's DB session: mints it, binds the context, commits on success and
    rolls back on failure. Handlers downstream must not commit.
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

        # Every token check a client makes counts against its per-minute limit, counted
        # before the lookup so a concurrent burst cannot outrun it. A token proven in the
        # last minute skips the count, so a working agent is never refused because
        # someone else probed from its address (FLEET-RATE-LIMIT-STANDARD §3).
        client_ip = client_ip_from_request(request)
        retry_after = (
            None
            if _is_recently_proven(token)
            else rate_limit.credential_attempts.retry_after(
                _MCP_AUTH_SURFACE, client_ip
            )
        )
        if retry_after is not None:
            response = JSONResponse(
                {"error": "Too many authentication attempts"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # worker_session is the app's non-request session owner: it commits on clean
        # exit and rolls back on exception.
        async with worker_session() as db:
            principal = await ApiTokenCoreService.authenticate(db, token)
            if principal is None:
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
            _mark_proven(token)

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

            set_request_context(db, principal)
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
