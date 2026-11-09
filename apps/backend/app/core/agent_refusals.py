"""Every refusal on the agent protocol carries the refusal header.

The agent believes a 401 or 403 only when the server marks it
(`app.shared.agent_protocol`): one without the mark comes from a proxy, a
gateway or another service in front, and the agent retries it as a temporary
failure. So no refusal of the server's may go unmarked, on any agent route.

Every router of the agent protocol declares `route_class=AgentProtocolRoute`.
Its handler, which resolves the route's dependencies too, marks each 401 and
403 the route raises or returns: with the value the refusal already carries
(the site that knows what it refused: the recovery secret, the agent's key,
the registration key), else `refused`. A new agent route is marked by being
on one of these routers, which a test holds for every `/api/v1/agent` route.

The cross-site guard (`app/core/same_origin.py`, an emitted asset) answers a
browser's cross-site POST before any route runs, so no route marks that 403.
`AgentRefusalMarkMiddleware`, added just outside the guard, marks every 401 and
403 the app answers on an agent-protocol path that does not carry the mark yet.
"""

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import Request, Response
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import LuxSwirlException
from app.core.openapi_operations import HandlerNamedRoute
from app.shared.agent_protocol import REFUSAL_HEADER, REFUSAL_STATUSES, REFUSED


def marked(headers: Mapping[str, str] | None) -> dict[str, str]:
    """`headers` with the refusal header, keeping a value already set."""
    marked_headers = dict(headers or {})
    marked_headers.setdefault(REFUSAL_HEADER, REFUSED)
    return marked_headers


class AgentProtocolRoute(HandlerNamedRoute):
    """An agent-protocol route: each 401 and 403 it answers is marked."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def marking_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as refusal:
                if refusal.status_code in REFUSAL_STATUSES:
                    refusal.headers = marked(refusal.headers)
                raise
            except LuxSwirlException as refusal:
                if refusal.status_code in REFUSAL_STATUSES:
                    refusal.headers = marked(refusal.headers)
                raise
            if response.status_code in REFUSAL_STATUSES:
                response.headers.setdefault(REFUSAL_HEADER, REFUSED)
            return response

        return marking_handler


# Where the agent protocol is served (app/api/v1/__init__.py).
AGENT_PROTOCOL_PATH = "/api/v1/agent"
_HEADER = REFUSAL_HEADER.lower().encode("latin-1")


def _on_agent_path(path: str) -> bool:
    return path == AGENT_PROTOCOL_PATH or path.startswith(f"{AGENT_PROTOCOL_PATH}/")


class AgentRefusalMarkMiddleware:
    """Marks each unmarked 401 and 403 the app answers on an agent-protocol path.

    The routes mark their own refusals (`AgentProtocolRoute`); this covers what
    answers before them, the cross-site guard. A value already set is kept.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _on_agent_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def marking_send(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] in REFUSAL_STATUSES
            ):
                headers = list(message.get("headers", []))
                if all(name.lower() != _HEADER for name, _ in headers):
                    headers.append((_HEADER, REFUSED.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, marking_send)
