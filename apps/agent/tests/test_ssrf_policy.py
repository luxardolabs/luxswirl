"""Every check enforces the server's SSRF policy when it connects.

The cloud-metadata range is always refused. Private networks are refused when
the server's `security.block_private_networks` is on: the checks list and every
heartbeat response carry it. A response without it leaves the policy as it is.

10.0.0.5 and 169.254.169.254 are literal IPs, so nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.agent import http_client
from app.agent.check_manager import CheckManager
from app.agent.heartbeat import HeartbeatSender
from app.checks import _ssrf_policy
from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.tcp import TCPCheck
from app.shared.ssrf import SsrfBlockedError

pytestmark = pytest.mark.unit

PRIVATE_IP = "10.0.0.5"


@pytest.fixture(autouse=True)
def _default_policy() -> Iterator[None]:
    _ssrf_policy.apply_server_policy({"block_private_networks": False})
    yield
    _ssrf_policy.apply_server_policy({"block_private_networks": False})


def _cfg(check_type: str, **overrides: Any) -> dict[str, Any]:
    return {
        "check_id": str(uuid4()),
        "name": "ssrf-policy",
        "check_type": check_type,
        "retries": 1,
        "timeout": 0.2,
        **overrides,
    }


_PRIVATE_CHECKS = [
    pytest.param(
        lambda: HTTPCheck(_cfg("http", target=f"http://{PRIVATE_IP}/")), id="http"
    ),
    pytest.param(lambda: TCPCheck(_cfg("tcp", target=PRIVATE_IP, port=5432)), id="tcp"),
    pytest.param(lambda: PingCheck(_cfg("ping", target=PRIVATE_IP)), id="ping"),
    pytest.param(
        lambda: DNSCheck(
            _cfg("dns", target="example.com", nameserver=PRIVATE_IP, record_type="A")
        ),
        id="dns-nameserver",
    ),
    pytest.param(
        lambda: MySQLCheck(
            _cfg(
                "mysql",
                target=f"mysql://u:p@{PRIVATE_IP}:3306/db",
                query="SELECT 1",
            )
        ),
        id="mysql",
    ),
    pytest.param(
        lambda: PostgreSQLCheck(
            _cfg(
                "postgres",
                target=f"postgresql://u:p@{PRIVATE_IP}:5432/db",
                query="SELECT 1",
            )
        ),
        id="postgres",
    ),
]


async def _blocked(check: Any) -> bool:
    """Whether the check refused its address as SSRF (raised, or a failed result
    naming the blocked range)."""
    try:
        result = await check.run()
    except SsrfBlockedError:
        return True
    return "private network" in (result.get("error") or "")


@pytest.mark.parametrize("make_check", _PRIVATE_CHECKS)
async def test_a_private_address_is_refused_when_the_server_blocks_them(
    make_check: Any,
) -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": True})

    assert await _blocked(make_check())


@pytest.mark.parametrize(
    "target",
    [f"http://{PRIVATE_IP}/", PRIVATE_IP, f"mysql://u:p@{PRIVATE_IP}:3306/db"],
)
def test_a_private_address_is_allowed_by_default(target: str) -> None:
    _ssrf_policy.assert_target_allowed(target)
    _ssrf_policy.assert_ip_allowed(PRIVATE_IP)


def test_a_response_without_the_policy_leaves_it_as_it_is() -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": True})

    _ssrf_policy.apply_server_policy({})
    _ssrf_policy.apply_server_policy({"block_private_networks": None})

    assert _ssrf_policy.block_private_networks() is True


def test_cloud_metadata_is_refused_whatever_the_server_says() -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": False})

    with pytest.raises(SsrfBlockedError):
        _ssrf_policy.assert_target_allowed("http://169.254.169.254/latest/")


def _mock_server(body: dict[str, Any], on_request: Any = None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if on_request is not None:
            on_request()
        return httpx.Response(200, json=body)

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )


async def test_the_checks_list_sets_the_policy() -> None:
    _mock_server({"checks": [], "block_private_networks": True})
    manager = CheckManager(
        config={
            "agent_id": "a1",
            "push_url": "https://server.example/api/v1/reports",
            "auth_key": "k",
        },
        cache_file=Path("/nonexistent"),
        credentials=None,
        on_orphaned_agent=None,
    )
    try:
        await manager.fetch_checks_from_server()
    finally:
        await http_client.aclose_all()

    assert _ssrf_policy.block_private_networks() is True


class _NoJobs:
    def get_stats(self) -> dict[str, Any]:
        return {}


async def test_a_heartbeat_response_sets_the_policy() -> None:
    sender = HeartbeatSender(
        config={
            "agent_id": "a1",
            "push_url": "https://server.example/api/v1/reports",
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
        job_processor=_NoJobs(),
        credentials=None,
        process=None,
        on_config_reload=None,
        on_agent_approved=None,
    )
    # One heartbeat: the server's answer also stops the loop.
    _mock_server(
        {"status": "ok", "block_private_networks": True}, on_request=sender._stop.set
    )
    try:
        await asyncio.wait_for(sender.send_heartbeat(), timeout=30)
    finally:
        await http_client.aclose_all()

    assert _ssrf_policy.block_private_networks() is True
