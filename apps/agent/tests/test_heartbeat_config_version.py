"""The agent re-fetches its checks whenever the server's version of its check
list changes.

The first heartbeat response is the baseline (the checks were loaded at
startup). Every change after it reloads, to or from None too: a distributed
check moving to or from this agent changes the version, and the agent must
pick it up or hand it back.
"""

from __future__ import annotations

import time

import pytest
from app.agent.heartbeat import HeartbeatSender

pytestmark = pytest.mark.unit


async def _reloads(versions: list[str | None]) -> int:
    reloads: list[int] = []

    async def reload() -> None:
        reloads.append(1)

    sender = HeartbeatSender(
        config={
            "agent_id": "a1",
            "server_url": "https://s.example",
            "auth_key": "k",
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
        on_config_reload=reload,
        on_agent_approved=None,
    )
    for version in versions:
        await sender._sync_checks({"config_version": version})
    return len(reloads)


async def test_the_first_version_is_the_baseline() -> None:
    assert await _reloads(["a" * 32]) == 0


async def test_a_changed_version_reloads() -> None:
    assert await _reloads(["a" * 32, "a" * 32, "b" * 32]) == 1


async def test_a_version_after_none_reloads() -> None:
    """A check moving to an agent whose list was empty."""
    assert await _reloads([None, None, "d" * 32]) == 1


async def test_none_after_a_version_reloads() -> None:
    """The check moving back off it."""
    assert await _reloads([None, "d" * 32, None]) == 2
