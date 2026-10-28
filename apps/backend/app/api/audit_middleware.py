"""The admin API's call audit: one row per authenticated request to /api/v1/admin.

`require_api_user` (app/api/deps.py) marks a request once its token authenticates,
with the token, its user and the matched route's TEMPLATE. This middleware, around
the routes, times the request, takes the status from the response it sends, and
writes the row after the response in a transaction of its own (`worker_session`),
as `MCPAuthMiddleware` audits /mcp: the request's own rollback never loses the row,
and a failed audit write is logged and never fails the request. An unauthenticated
request is never marked, so it leaves no row. The raw path, the query string and
the body are never read.
"""

import re
import time
from dataclasses import dataclass
from uuid import UUID

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_helpers import client_ip_from_request
from app.db import worker_session
from app.models.enum_model import HttpMethod
from app.schemas.api_token_schema import ApiPrincipal
from app.services.core.api_audit_core_service import ApiAuditCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.api.audit")

# The surface audited: every admin API route is under it.
ADMIN_API_PREFIX = "/api/v1/admin"
# Where `require_api_user` leaves the authenticated caller, in the request state.
_STATE_KEY = "admin_api_audited_caller"


@dataclass(frozen=True)
class AuditedCaller:
    """Who an authenticated admin API request acts as, and the route it matched."""

    user_id: UUID
    api_token_id: UUID
    route: str


def route_template(path: str, route: object) -> str:
    """The template `path` was matched by: the including routers' literal prefix,
    then the route's own template (`/api/v1/admin` + `/checks/{check_id}`).

    The matched route knows its template only from its own router down (the
    prefixes of the routers including it are not part of it), so the prefix is
    the longest leading part of `path` the route's pattern does not cover. A
    prefix is literal, never a parameter. Without a matched route, the surface.
    """
    template = getattr(route, "path", None)
    pattern = getattr(route, "path_regex", None)
    if not isinstance(template, str) or not isinstance(pattern, re.Pattern):
        return ADMIN_API_PREFIX
    segments = path.split("/")
    for start in range(1, len(segments)):
        if pattern.match("/" + "/".join(segments[start:])):
            return "/".join(segments[:start]) + template
    return ADMIN_API_PREFIX


def mark_authenticated(request: Request, principal: ApiPrincipal) -> None:
    """Record that `principal` authenticated `request`, for its audit row."""
    setattr(
        request.state,
        _STATE_KEY,
        AuditedCaller(
            user_id=principal.user.id,
            api_token_id=principal.token_id,
            route=route_template(request.url.path, request.scope.get("route")),
        ),
    )


class AdminApiAuditMiddleware:
    """ASGI middleware writing each authenticated admin API request's audit row."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == ADMIN_API_PREFIX or path.startswith(f"{ADMIN_API_PREFIX}/")
        ):
            await self.app(scope, receive, send)
            return

        # The dict the routes' request state lives in, made now so this layer and
        # the dependency share it.
        state: dict[str, object] = scope.setdefault("state", {})
        start = time.monotonic()
        status_code = 500

        async def send_and_note_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_and_note_status)
        finally:
            caller = state.get(_STATE_KEY)
            if isinstance(caller, AuditedCaller):
                await _audit(scope, caller, status_code, start)


async def _audit(
    scope: Scope, caller: AuditedCaller, status_code: int, start: float
) -> None:
    """Write the row, in a transaction of its own. Best-effort: a failure is logged
    and never reaches the caller."""
    try:
        request = Request(scope)
        async with worker_session() as db:
            await ApiAuditCoreService.log_call(
                db,
                user_id=caller.user_id,
                api_token_id=caller.api_token_id,
                # Every admin route answers one of these; any other method
                # matches no route, so its request never authenticates.
                method=HttpMethod(request.method),
                route=caller.route,
                status_code=status_code,
                duration_ms=int((time.monotonic() - start) * 1000),
                ip_address=client_ip_from_request(request),
                user_agent=request.headers.get("user-agent"),
            )
    except Exception:
        logger.exception("Failed to log admin API call", extra={"route": caller.route})
