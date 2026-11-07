"""Every check enforces the server's SSRF policy when it connects.

The cloud-metadata range is always refused. Private networks are refused when
the server's `security.block_private_networks` is on: the checks list and every
heartbeat response carry it. A response without it leaves the policy as it is.

10.0.0.5 and 169.254.169.254 are literal IPs, so nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.agent import http_client
from app.agent.check_manager import CheckManager
from app.agent.credentials import AgentCredentials
from app.agent.heartbeat import HeartbeatSender
from app.checks import _ssrf_policy
from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.tcp import TCPCheck
from app.shared import ssrf
from app.shared.ssrf import SsrfBlockedError
from asyncpg import connect_utils

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
            "server_url": "https://server.example",
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
            "server_url": "https://server.example",
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
        credentials=AgentCredentials(),
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


async def _hosts_dialed(check: PostgreSQLCheck, monkeypatch: Any) -> list[str]:
    """Run the check and record each host asyncpg opens a socket to (refused,
    so nothing leaves the box)."""
    dialed: list[str] = []
    loop = asyncio.get_running_loop()

    async def refuse(_factory: Any, host: Any = None, *a: Any, **k: Any) -> Any:
        dialed.append(str(host))
        raise ConnectionRefusedError("test")

    monkeypatch.setattr(loop, "create_connection", refuse)
    with contextlib.suppress(SsrfBlockedError):
        await check.run()
    return dialed


def _postgres(dsn: str) -> PostgreSQLCheck:
    return PostgreSQLCheck(
        _cfg("postgres", target="192.0.2.10", connection_string=dsn, query="SELECT 1")
    )


async def test_postgres_dials_a_public_host(monkeypatch: Any) -> None:
    """Control: the recorder sees what asyncpg dials."""
    assert "192.0.2.10" in await _hosts_dialed(
        _postgres("postgresql://u:p@192.0.2.10/db"), monkeypatch
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@/db?host=169.254.169.254",
        "postgresql://u:p@192.0.2.10,169.254.169.254/db",
        "postgres:?host=169.254.169.254&port=80",
        "postgresql:/db?hostaddr=169.254.169.254",
        "postgres:?host=169.254.169.254:x:y&port=80",
        "postgresql://u:p@/db?host=169.254.169.254:x:y&port=80",
        "postgresql://a@169.254.169.254,b@192.0.2.10/db",
        "postgres:?host=[::ffff:169.254.169.254%2525]&port=80",
        "postgresql://u@/db?host=[fe80::1%2525]",
    ],
    ids=[
        "host-param",
        "multi-host",
        "no-slashes",
        "no-slashes-hostaddr",
        "no-slashes-colons",
        "host-param-colons",
        "userinfo-per-host",
        "scoped-no-slashes",
        "scoped",
    ],
)
async def test_postgres_never_dials_a_metadata_host_its_url_hides(
    dsn: str, monkeypatch: Any
) -> None:
    pytest.importorskip("asyncpg")
    dialed = await _hosts_dialed(_postgres(dsn), monkeypatch)
    assert not [host for host in dialed if "169.254" in host or "fe80" in host]


@pytest.mark.parametrize(
    "target",
    [
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "::ffff:169.254.169.254",
        "::ffff:a9fe:a9fe",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
    ],
)
def test_an_ipv6_form_of_the_metadata_address_is_refused(target: str) -> None:
    with pytest.raises(SsrfBlockedError):
        _ssrf_policy.assert_target_allowed(target)


def test_an_ipv6_form_of_a_private_address_follows_the_policy() -> None:
    _ssrf_policy.assert_ip_allowed("::ffff:10.0.0.5")
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    _ssrf_policy.assert_ip_allowed("1.1.1.1")
    with pytest.raises(SsrfBlockedError):
        _ssrf_policy.assert_ip_allowed("::ffff:10.0.0.5")


async def test_an_agent_restarted_offline_keeps_the_servers_policy(
    tmp_path: Path,
) -> None:
    config = {
        "agent_id": "a1",
        "server_url": "https://server.example",
        "auth_key": "k",
    }
    cache = tmp_path / "checks_cache.json"
    _mock_server({"checks": [], "block_private_networks": True})
    try:
        await CheckManager(
            config=config, cache_file=cache, credentials=None, on_orphaned_agent=None
        ).load_checks()
    finally:
        await http_client.aclose_all()

    # A fresh process, and the server unreachable.
    _ssrf_policy.apply_server_policy({"block_private_networks": False})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server down")

    http_client._server_client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    try:
        await CheckManager(
            config=config, cache_file=cache, credentials=None, on_orphaned_agent=None
        ).load_checks()
    finally:
        await http_client.aclose_all()

    assert _ssrf_policy.block_private_networks() is True


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "0.0.0.0", "100.64.0.1"])
def test_the_agent_host_itself_follows_the_policy(ip: str) -> None:
    _ssrf_policy.assert_ip_allowed(ip)
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    with pytest.raises(SsrfBlockedError):
        _ssrf_policy.assert_ip_allowed(ip)


async def test_a_mysql_url_naming_no_host_is_refused_before_it_connects() -> None:
    """aiomysql would connect a hostless URL to localhost."""
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    check = MySQLCheck(_cfg("mysql", target="mysql://root:pw@/db", query="SELECT 1"))

    assert await _blocked(check)


@pytest.mark.parametrize("dsn", ["postgres:", "postgres:/db"])
async def test_postgres_with_no_slashes_is_held_to_the_private_setting(
    dsn: str, monkeypatch: Any
) -> None:
    pytest.importorskip("asyncpg")
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    try:
        assert await _hosts_dialed(_postgres(dsn), monkeypatch) == []
    finally:
        _ssrf_policy.apply_server_policy({"block_private_networks": False})


@pytest.mark.parametrize("dsn", ["mysql:d", "mysql:"])
async def test_mysql_with_no_slashes_is_held_to_the_private_setting(dsn: str) -> None:
    pytest.importorskip("aiomysql")
    check = MySQLCheck(
        _cfg("mysql", target="192.0.2.10", connection_string=dsn, query="SELECT 1")
    )
    connects: list[Any] = []

    async def connect(**kwargs: Any) -> None:
        connects.append(kwargs)
        raise ConnectionRefusedError("test")

    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("app.checks.mysql.aiomysql.connect", connect)
            with contextlib.suppress(SsrfBlockedError):
                await check.run()
    finally:
        _ssrf_policy.apply_server_policy({"block_private_networks": False})

    assert connects == []


def test_an_endpoint_never_shows_a_mangled_urls_password() -> None:
    host, port = PostgreSQLCheck._endpoint("postgres:/\t/u:s3cret@a:5432,b:5433/db")

    assert "s3cret" not in host
    assert (host, port) == ("a,b", None)


def _driver_hosts(dsn: str) -> list[str]:
    addresses, _ = connect_utils._parse_connect_dsn_and_args(
        dsn=dsn,
        host=None,
        port=None,
        user="u",
        password="p",
        passfile=None,
        database="d",
        ssl=None,
        service=None,
        servicefile=None,
        direct_tls=None,
        server_settings=None,
        target_session_attrs=None,
        krbsrvname=None,
        gsslib=None,
    )
    return [address[0] for address in addresses if isinstance(address, tuple)]


def test_every_host_asyncpg_reads_is_a_host_the_policy_names() -> None:
    """The driver's own parser against the policy's, over generated URLs: of every URL
    the policy reads, the driver connects only to hosts it names, so an asyncpg upgrade
    that reads a host another way fails here. Parsing only; nothing connects."""
    pieces = [
        "169.254.169.254",
        "192.0.2.10",
        "::ffff:169.254.169.254",
        "fe80::1",
        "%2525",
        "%2531",
        ":",
        ",",
        "@",
        "/",
        "?",
        "&",
        "=",
        "#",
        "[",
        "]",
        "%",
        "%2C",
        "%3A",
        "%40",
        "%2F",
        "%20",
        "%25",
        "host=",
        "hostaddr=",
        "port=80",
        "port=",
        "u",
        "p",
        "x",
        "y",
        "80",
        "5432",
        "db",
        "service=s",
        "user=u",
    ] + ["\t", "\n", " ", "\\", "+", ";", "\x01"]
    prefixes = [
        "postgres:",
        "postgres:/",
        "postgres://",
        "postgresql:?",
        "postgres:?host=",
        "postgresql://u:p@/db?host=",
        "POSTGRES:?host=",
        "post\tgres:?host=",
        " postgres:?host=",
        "postgres:/\t/",
        "postgres:\t//",
        "postgres://u@",
        "postgres:?port=80&host=",
        "postgresql:///d?port=80&host=",
        "postgres:d?host=",
    ]
    rnd = random.Random(508)
    missed: list[tuple[str, str]] = []
    accepted = 0
    for _ in range(40_000):
        dsn = rnd.choice(prefixes) + "".join(
            rnd.choice(pieces) for _ in range(rnd.randint(0, 7))
        )
        try:
            hosts = _driver_hosts(dsn)
        except ValueError, IndexError:
            continue
        try:
            named = ssrf.extract_hosts(dsn, database=True)
        except SsrfBlockedError:
            continue
        accepted += 1
        # An empty host is one no resolver takes.
        missed.extend((dsn, host) for host in hosts if host and host not in named)

    assert accepted > 1_000
    assert missed == []
