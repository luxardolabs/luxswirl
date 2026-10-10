"""The heartbeat rides the agent's one process-lifetime server client.

It used to build its own `httpx.AsyncClient` (and the shutdown heartbeat a
second, throwaway one), paying connection and TLS setup outside the shared pool
and baking the auth key into the client at startup. Now each request carries the
key the agent holds at that moment, and closing is left to `aclose_all()`.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from app.agent import http_client
from app.agent.heartbeat import HeartbeatSender

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def seen() -> AsyncIterator[list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        yield requests
    finally:
        await http_client.aclose_all()


def _sender(auth_key: str) -> HeartbeatSender:
    return HeartbeatSender(
        config={
            "agent_id": "a1",
            "push_url": "https://server.example/api/v1/reports",
            "auth_key": auth_key,
        },
        metrics={
            "start_time": time.time(),
            "checks_executed": 0,
            "checks_succeeded": 0,
            "checks_failed": 0,
            "result_queue_max_size": 0,
            "heartbeats": 0,
        },
        reporter=None,
        job_processor=None,
        credentials=None,
        process=None,
        on_config_reload=None,
        on_agent_approved=None,
    )


async def test_shutdown_heartbeat_uses_the_shared_client_and_leaves_it_open(
    seen: list[httpx.Request],
) -> None:
    shared = http_client.server_client()

    await _sender("k1").send_shutdown_heartbeat("run-1")

    assert [str(r.url) for r in seen] == ["https://server.example/api/v1/heartbeat"]
    assert not shared.is_closed


async def test_each_heartbeat_sends_the_key_held_at_that_moment(
    seen: list[httpx.Request],
) -> None:
    sender = _sender("registration-key")
    await sender.send_shutdown_heartbeat("run-1")
    sender.config["auth_key"] = "agent-key"
    await sender.send_shutdown_heartbeat("run-1")

    assert [r.headers["authorization"] for r in seen] == [
        "Bearer registration-key",
        "Bearer agent-key",
    ]
