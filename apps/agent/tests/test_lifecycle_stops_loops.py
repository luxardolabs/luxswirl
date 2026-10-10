"""Shutdown stops the agent's loops instead of waiting them out.

Nothing ever set the heartbeat sender's (or the result processor's, or the
health monitor's) `running` flag back to False, so on every shutdown the
heartbeat loop kept going: the lifecycle waited its whole `shutdown_timeout`
for a task that could not finish, and a regular "online" heartbeat could still
go out after the "offline" shutdown heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.agent import http_client
from app.agent.heartbeat import HeartbeatSender
from app.agent.lifecycle import LifecycleManager

pytestmark = pytest.mark.unit

SHUTDOWN_TIMEOUT = 3.0


class _Loop:
    """A stand-in for a loop the lifecycle must stop."""

    def __init__(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


class _Stoppable:
    def get_metrics(self) -> dict[str, Any]:
        return {}

    def get_stats(self) -> dict[str, Any]:
        return {}

    async def stop(self) -> None:
        return None


@pytest_asyncio.fixture
async def posted() -> AsyncIterator[list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        yield bodies
    finally:
        await http_client.aclose_all()


def _sender() -> HeartbeatSender:
    return HeartbeatSender(
        config={
            "agent_id": "a1",
            "push_url": "https://server.example",
            "auth_key": "k",
            "heartbeat_interval": 60,
            "resource_monitoring_enabled": False,
        },
        metrics={
            "start_time": time.time(),
            "checks_executed": 0,
            "checks_succeeded": 0,
            "checks_failed": 0,
            "result_queue_max_size": 0,
            "heartbeats": 0,
        },
        reporter=_Stoppable(),
        job_processor=_Stoppable(),
        credentials=None,
        process=None,
        on_config_reload=None,
        on_agent_approved=None,
    )


async def test_shutdown_stops_the_heartbeat_loop_and_it_reports_offline_last(
    posted: list[dict[str, Any]],
) -> None:
    sender = _sender()
    result_processor, health_monitor = _Loop(), _Loop()
    lifecycle = LifecycleManager(
        config={},
        metrics={},
        result_queue=asyncio.Queue(),
        reporter=_Stoppable(),
        job_processor=_Stoppable(),
        heartbeat_sender=sender,
        result_processor=result_processor,
        health_monitor=health_monitor,
        run_id="run-1",
        shutdown_timeout=SHUTDOWN_TIMEOUT,
    )
    sender.start()
    lifecycle.running = True
    task = asyncio.create_task(sender.send_heartbeat())
    lifecycle.heartbeat_task = task
    await asyncio.sleep(0.2)  # into its interval wait

    started = time.monotonic()
    await lifecycle.shutdown()
    elapsed = time.monotonic() - started

    assert task.done()
    assert elapsed < SHUTDOWN_TIMEOUT / 2
    assert not result_processor.running
    assert not health_monitor.running
    assert posted[-1]["status"] == "offline"
