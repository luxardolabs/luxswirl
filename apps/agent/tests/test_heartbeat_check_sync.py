"""The agent re-fetches its checks once `check_sync_interval` has passed since
they were last loaded, even when their version has not changed.

The interval comes from each heartbeat response (the agent's own, else the
server default). A reload for a changed version restarts the wait.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.agent.heartbeat import HeartbeatSender

pytestmark = pytest.mark.unit

VERSION = "a" * 32


def _sender(reloads: list[int]) -> HeartbeatSender:
    async def reload() -> None:
        reloads.append(1)

    return HeartbeatSender(
        config={
            "agent_id": "a1",
            "push_url": "https://s.example/api/v1/reports",
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


def _response(**fields: Any) -> dict[str, Any]:
    return {"config_version": VERSION, "check_sync_interval": 60, **fields}


async def test_an_unchanged_version_reloads_once_the_interval_has_passed() -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    await sender._sync_checks(_response())

    sender.last_check_sync -= 61
    await sender._sync_checks(_response())

    assert len(reloads) == 1


async def test_an_unchanged_version_within_the_interval_does_not_reload() -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    await sender._sync_checks(_response())

    sender.last_check_sync -= 59
    await sender._sync_checks(_response())

    assert reloads == []


async def test_the_interval_is_the_one_the_server_sends() -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    await sender._sync_checks(_response(check_sync_interval=600))

    sender.last_check_sync -= 61
    await sender._sync_checks(_response(check_sync_interval=600))
    assert reloads == []

    sender.last_check_sync -= 540
    await sender._sync_checks(_response(check_sync_interval=600))
    assert len(reloads) == 1


async def test_a_sync_restarts_the_wait() -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    await sender._sync_checks(_response())
    sender.last_check_sync -= 61
    await sender._sync_checks(_response())

    await sender._sync_checks(_response())

    assert len(reloads) == 1


async def test_a_reload_for_a_changed_version_restarts_the_wait() -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    await sender._sync_checks(_response())
    sender.last_check_sync -= 61

    await sender._sync_checks(_response(config_version="b" * 32))
    await sender._sync_checks(_response(config_version="b" * 32))

    assert len(reloads) == 1


async def test_a_response_without_a_version_still_syncs_when_due() -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    sender.last_check_sync -= 61

    await sender._sync_checks({"check_sync_interval": 60})

    assert len(reloads) == 1


@pytest.mark.parametrize("interval", [None, 0, -5, "30", True, 1.5])
async def test_an_unusable_interval_keeps_the_one_in_use(interval: object) -> None:
    reloads: list[int] = []
    sender = _sender(reloads)
    await sender._sync_checks(_response(check_sync_interval=600))

    await sender._sync_checks(_response(check_sync_interval=interval))

    assert sender.check_sync_interval == 600
