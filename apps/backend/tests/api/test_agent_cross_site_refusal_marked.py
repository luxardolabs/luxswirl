"""The same-origin guard's 403 on an agent-protocol path is marked as a refusal.

The agent believes a 401 or 403 only when it carries `X-LuxSwirl-Refused`
(`app.shared.agent_protocol`). The cross-site guard (`app/core/same_origin.py`,
an emitted asset) answers a browser's cross-site POST before any route runs, so
the route class that marks every other refusal never sees it. The app marks
that answer around the guard, on agent paths only, keeping any value already
set. Through the real app, so a wrapper that was not installed fails here.
"""

from __future__ import annotations

import pytest
from app.core.agent_refusals import AgentRefusalMarkMiddleware
from app.core.config import settings
from app.main import app
from app.shared.agent_protocol import REFUSAL_HEADER, REFUSED
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

pytestmark = pytest.mark.integration

OWN = settings.allowed_hosts[0]

CROSS_SITE = [
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
]

AGENT_POSTS = [
    "/api/v1/agent/heartbeat",
    "/api/v1/agent/register",
    "/api/v1/agent/reports",
    "/api/v1/agent/artifacts",
    f"/api/v1/agent/{'0' * 8}-0000-0000-0000-{'0' * 12}/recover-key",
    "/api/v1/agent/no-such-route",
]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=f"https://{OWN}")


@pytest.mark.parametrize("headers", CROSS_SITE)
@pytest.mark.parametrize("path", AGENT_POSTS)
async def test_a_cross_site_refusal_on_an_agent_path_is_marked(
    path: str, headers: dict[str, str]
) -> None:
    async with _client() as c:
        r = await c.post(path, headers=headers, json={})

    assert r.status_code == 403
    assert r.headers[REFUSAL_HEADER] == REFUSED
    assert r.headers["Cache-Control"] == "no-store"  # the guard's own headers kept


@pytest.mark.parametrize(
    "path", ["/login", "/logout", "/api/v1/agentx", "/api/v1/admin/me", "/mcp"]
)
async def test_a_cross_site_refusal_elsewhere_is_not_marked(path: str) -> None:
    async with _client() as c:
        r = await c.post(path, headers={"Sec-Fetch-Site": "cross-site"})

    assert r.status_code == 403
    assert REFUSAL_HEADER not in r.headers


async def _answer(status: int, headers: list[tuple[bytes, bytes]]) -> list[Message]:
    """What the wrapper sends for an inner app answering ``status`` on an agent path."""

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": b""})

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    scope: Scope = {"type": "http", "path": "/api/v1/agent/heartbeat", "method": "POST"}
    await AgentRefusalMarkMiddleware(inner)(scope, receive, send)
    return sent


async def test_a_value_already_set_is_kept_and_not_doubled() -> None:
    sent = await _answer(401, [(b"x-luxswirl-refused", b"agent-key")])

    marks = [v for k, v in sent[0]["headers"] if k.lower() == b"x-luxswirl-refused"]
    assert marks == [b"agent-key"]


@pytest.mark.parametrize("status", [200, 202, 400, 404, 422, 429, 500])
async def test_a_status_that_is_no_refusal_is_not_marked(status: int) -> None:
    sent = await _answer(status, [])

    assert all(k.lower() != b"x-luxswirl-refused" for k, _ in sent[0]["headers"])
