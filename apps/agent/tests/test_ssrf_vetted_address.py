"""Checks connect to an address the SSRF policy vetted, never to a name resolved again.

A host that resolves to an allowed address and a blocked one (a Pi-hole answering
with its IPv6 link-local address beside its LAN address) is reached on the allowed
one; a host with no allowed address is refused; and the address a check connects
to is the one it vetted, so a resolver that changes its answer in between (DNS
rebinding) never reaches a blocked range.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections import defaultdict
from typing import Any
from uuid import uuid4

import httpcore
import pytest
from app.agent import http_client
from app.agent.check_executor import CheckExecutor
from app.checks._ssrf_http import VettedTransport
from app.checks.base import BaseCheck
from app.checks.http import HTTPCheck
from app.checks.ping import PingCheck
from app.checks.tcp import TCPCheck
from app.shared import ssrf
from app.shared.ssrf import SsrfBlockedError

pytestmark = pytest.mark.unit

LINK_LOCAL = "fe80::be24:11ff:fea6:7aca"
METADATA = "169.254.169.254"


def _infos(*ips: str, port: int = 0) -> list[tuple[Any, ...]]:
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        out.append((family, socket.SOCK_STREAM, 6, "", address))
    return out


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Names the test resolves, and their addresses in resolver order."""
    table: dict[str, list[str]] = {}
    real = socket.getaddrinfo

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name in table:
            return _infos(*table[name], port=int(port or 0))
        return real(host, port, *args, **kwargs)

    async def fake_async(
        self: Any, host: Any, port: Any, *args: Any, **kwargs: Any
    ) -> Any:
        return fake(host, port)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_async)
    return table


@pytest.fixture(autouse=True)
async def fresh_clients() -> Any:
    yield
    await http_client.aclose_all()


def _cfg(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "check_id": str(uuid4()),
        "name": "pihole",
        "check_type": "ping",
        "retries": 1,
        "timeout": 2,
    }
    cfg.update(overrides)
    return cfg


# ── The shared rule ──────────────────────────────────────────────────────────


def test_allowed_addresses_skips_a_blocked_one(answers) -> None:
    answers["pi.hole"] = [LINK_LOCAL, "192.168.1.5"]
    assert ssrf.allowed_addresses("pi.hole") == ["192.168.1.5"]


def test_allowed_addresses_keeps_the_resolver_order(answers) -> None:
    answers["two.test"] = ["192.168.1.9", "192.168.1.5", "192.168.1.9"]
    assert ssrf.allowed_addresses("two.test") == ["192.168.1.9", "192.168.1.5"]


def test_a_host_with_only_blocked_addresses_is_refused(answers) -> None:
    answers["evil.test"] = [METADATA, LINK_LOCAL]
    with pytest.raises(SsrfBlockedError, match="169.254"):
        ssrf.allowed_addresses("evil.test")


def test_a_host_that_does_not_resolve_has_no_addresses(answers) -> None:
    assert ssrf.allowed_addresses("nowhere.invalid") == []


def test_private_networks_blocked_leaves_no_address(answers) -> None:
    answers["lan.test"] = ["192.168.1.5"]
    with pytest.raises(SsrfBlockedError, match="private"):
        ssrf.allowed_addresses("lan.test", block_private_networks=True)


# ── ping ─────────────────────────────────────────────────────────────────────


async def test_ping_pings_the_allowed_address_not_the_name(
    answers, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers["pi.hole"] = [LINK_LOCAL, "192.168.1.5"]
    seen: list[tuple[str, ...]] = []

    async def fake_run(*args: str, **kwargs: Any) -> tuple[int, bytes, bytes]:
        seen.append(args)
        return (
            0,
            b"1 packets transmitted, 1 received, 0% packet loss\n"
            b"rtt min/avg/max/mdev = 0.2/0.2/0.2/0.0 ms",
            b"",
        )

    monkeypatch.setattr("app.checks.ping.run_subprocess_safely", fake_run)
    result = await PingCheck(_cfg(target="pi.hole")).run()

    assert result["success"] is True, result
    assert seen and seen[0][-1] == "192.168.1.5", seen
    assert "pi.hole" not in seen[0]


async def test_ping_refuses_a_host_with_no_allowed_address(answers) -> None:
    answers["evil.test"] = [METADATA]
    with pytest.raises(SsrfBlockedError):
        await PingCheck(_cfg(target="evil.test")).run()


# ── tcp ──────────────────────────────────────────────────────────────────────


async def test_tcp_connects_on_the_allowed_address(answers) -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answers["pi.hole"] = [LINK_LOCAL, "127.0.0.1"]
    async with server:
        result = await TCPCheck(
            _cfg(check_type="tcp", target="pi.hole", port=port)
        ).run()
    assert result["success"] is True, result


async def test_tcp_refuses_a_host_with_no_allowed_address(answers) -> None:
    answers["evil.test"] = [METADATA]
    result = await TCPCheck(_cfg(check_type="tcp", target="evil.test", port=80)).run()
    assert result["success"] is False
    assert "169.254" in (result.get("error") or ""), result


# ── http ─────────────────────────────────────────────────────────────────────


async def _http_server() -> tuple[asyncio.Server, int, list[bytes]]:
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], requests


async def test_http_fetches_on_the_allowed_address_with_the_name_as_host(
    answers,
) -> None:
    server, port, requests = await _http_server()
    answers["pi.hole"] = [LINK_LOCAL, "127.0.0.1"]
    async with server:
        result = await HTTPCheck(
            _cfg(
                check_type="http", target=f"http://pi.hole:{port}/", expected_status=200
            )
        ).run()
    assert result["success"] is True, result
    assert requests and f"host: pi.hole:{port}".encode() in requests[0].lower()


async def test_http_connects_to_the_address_it_vetted(
    answers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket is opened on the vetted IP, never on the name, so nothing
    resolves the name again between the check and the connection."""
    server, port, _ = await _http_server()
    answers["pi.hole"] = [LINK_LOCAL, "127.0.0.1"]
    opened: list[str] = []
    real = httpcore.AnyIOBackend.connect_tcp

    async def spy(self: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(host)
        return await real(self, host, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", spy)
    async with server:
        await HTTPCheck(
            _cfg(
                check_type="http", target=f"http://pi.hole:{port}/", expected_status=200
            )
        ).run()
    assert opened == ["127.0.0.1"]


async def test_http_refuses_a_host_with_no_allowed_address(answers) -> None:
    answers["evil.test"] = [METADATA]
    result = await HTTPCheck(
        _cfg(check_type="http", target="http://evil.test/", expected_status=200)
    ).run()
    assert result["success"] is False
    assert "169.254" in (result.get("error") or ""), result


async def test_http_redirect_to_a_blocked_host_is_refused(answers) -> None:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 302 Found\r\nLocation: http://evil.test/latest/meta-data/\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answers["start.test"] = ["127.0.0.1"]
    answers["evil.test"] = [METADATA]
    async with server:
        result = await HTTPCheck(
            _cfg(
                check_type="http",
                target=f"http://start.test:{port}/",
                expected_status=200,
            )
        ).run()
    assert result["success"] is False
    assert "169.254" in (result.get("error") or ""), result


@pytest.mark.parametrize("verify", [True, False])
def test_the_vetted_pool_keeps_the_tls_setting(verify: bool) -> None:
    """The pool the transport builds verifies certificates and names exactly
    as httpx's own would for the same `verify`."""
    context = VettedTransport(verify=verify)._pool._ssl_context
    assert context is not None
    if verify:
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
    else:
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False


# ── a refusal is a result, not an error ─────────────────────────────────────


class _Refused(BaseCheck):
    def validate_config(self, config: dict[str, Any]) -> None:
        return None

    async def run(self) -> dict[str, Any]:
        raise SsrfBlockedError(f"{METADATA} is in the blocked cloud metadata range.")


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def test_a_refused_check_logs_a_warning_without_a_traceback() -> None:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    executor = CheckExecutor(
        config={"agent_id": "a1"},
        metrics=defaultdict(int),
        check_stats=defaultdict(dict),
        semaphore=asyncio.Semaphore(1),
        result_queue=queue,
        last_state={},
        run_id="r1",
    )
    check = _Refused(_cfg(target=METADATA))
    logger = logging.getLogger(executor.logger.name)
    handler = _Records()
    logger.addHandler(handler)
    try:
        result, _ = await executor._run(check, timeout=1, longest=0)
    finally:
        logger.removeHandler(handler)

    assert result["success"] is False
    assert "169.254" in result["error"]
    assert not [r for r in handler.records if r.levelno >= logging.ERROR]
    assert all(r.exc_info is None for r in handler.records)
    assert any(r.levelno == logging.WARNING for r in handler.records)
