"""More ways the checks' lookup pool could go wrong, each held to.

A target that dns, postgres and mysql vetted under one SSRF policy is judged again
when the server changes it mid-lookup; an HTTPS check reads its certificate only
after its request answered, on addresses judged under the policy in force when it
connects; many HTTPS checks on a slow domain leave a healthy lookup working; and
every lookup timeout names its host and is not retried.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

import httpcore
import pytest
from app.agent import http_client
from app.agent.check_executor import CheckExecutor
from app.checks import _lookups, _ssrf_policy, postgres
from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.tcp import TCPCheck
from app.shared.ssrf import SsrfBlockedError

pytestmark = pytest.mark.unit

PRIVATE = "10.0.0.5"


def _executor() -> CheckExecutor:
    return CheckExecutor(
        config={"agent_id": "a1"},
        metrics=defaultdict(int),
        check_stats=defaultdict(dict),
        semaphore=asyncio.Semaphore(500),
        result_queue=asyncio.Queue(),
        last_state={},
        run_id="r1",
    )


def _cfg(check_type: str, target: str, **kw: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "check_id": str(uuid4()),
        "name": f"{check_type}-adv2",
        "check_type": check_type,
        "target": target,
        "retries": 1,
    }
    cfg.update(kw)
    return cfg


class Dns:
    """Gated hosts answer PRIVATE once the gate opens; healthy.test answers at once."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.asked: list[str] = []
        self.lock = threading.Lock()

    def asked_for(self, host: str) -> int:
        with self.lock:
            return self.asked.count(host)


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = Dns()

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host)
        p = int(port or 0)
        if name == "healthy.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", p))]
        if name.endswith(".gated.test"):
            with state.lock:
                state.asked.append(name)
            state.gate.wait(20)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE, p))]
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setitem(_ssrf_policy._policy, "block_private_networks", False)
    yield state
    state.gate.set()


@pytest.fixture
def no_connections(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every outbound connection attempt is recorded and refused, never made."""
    made: list[Any] = []

    def create_connection(address: Any, *a: Any, **k: Any) -> Any:
        made.append(("probe",) + tuple(address))
        raise OSError("probe: connection recorded, not made")

    async def connect_tcp(self: Any, host: str, port: int, *a: Any, **k: Any) -> Any:
        made.append(("request", host, port))
        raise httpcore.ConnectError("probe: connection recorded, not made")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    monkeypatch.setattr(http_client, "_check_clients", {})
    return made


async def _until(cond: Any, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.01)


async def _drained(seconds: float = 10) -> None:
    await _until(lambda: not _lookups._in_flight, seconds)


async def _close_clients() -> None:
    for client in http_client._check_clients.values():
        await client.aclose()


# --- dns, postgres and mysql: a policy turned on mid-lookup holds ---------------

PG = "postgresql://u:p@db.gated.test:5432/app"


async def test_a_target_vetted_while_the_policy_turned_on_is_refused(
    dns: Dns,
) -> None:
    task = asyncio.create_task(_ssrf_policy.target_allowed(PG, database=True))
    await _until(lambda: dns.asked_for("db.gated.test") >= 1)
    _ssrf_policy.apply_server_policy({"block_private_networks": True})  # heartbeat
    dns.gate.set()
    with pytest.raises(SsrfBlockedError):
        await task


async def test_a_postgres_check_never_connects_after_the_policy_turned_on(
    dns: Dns, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialed: list[str] = []

    async def connect(*, dsn: str, timeout: float) -> Any:
        dialed.append(dsn)
        raise OSError("probe: driver connect recorded, not made")

    monkeypatch.setattr(postgres.asyncpg, "connect", connect)
    check = PostgreSQLCheck(_cfg("postgres", PG, timeout=2))
    task = asyncio.create_task(_executor()._run(check, timeout=10, longest=0))
    await _until(lambda: dns.asked_for("db.gated.test") >= 1)
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    dns.gate.set()
    result, _ = await task
    assert dialed == [], f"driver dialled a private host under block: {result}"


async def test_a_target_under_the_policy_is_refused(dns: Dns) -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    dns.gate.set()
    with pytest.raises(SsrfBlockedError):
        await _ssrf_policy.target_allowed(PG, database=True)


# --- The certificate read: only after an answer, only on allowed addresses ------

WEB = "https://web.gated.test/"


async def test_a_refused_request_reads_no_certificate_after_the_policy_turned_on(
    dns: Dns, no_connections: list[Any]
) -> None:
    check = HTTPCheck(_cfg("http", WEB, timeout=5))
    task = asyncio.create_task(check.run())
    await _until(lambda: dns.asked_for("web.gated.test") >= 1)
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    dns.gate.set()
    result = await task
    await _drained()
    await asyncio.sleep(0.2)
    await _close_clients()
    assert result["success"] is False  # the request itself was refused
    assert ("probe", PRIVATE, 443) not in no_connections, (no_connections, result)


async def test_a_request_under_the_policy_connects_nowhere(
    dns: Dns, no_connections: list[Any]
) -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    dns.gate.set()
    result = await HTTPCheck(_cfg("http", WEB, timeout=5)).run()
    await _drained()
    await _close_clients()
    assert result["success"] is False
    assert no_connections == [], no_connections


def test_the_certificate_read_judges_each_address_as_it_connects(
    no_connections: list[Any],
) -> None:
    """Addresses allowed when looked up, then blocked by the time the read connects."""
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    try:
        assert HTTPCheck._read_certificate("web.test", 443, (PRIVATE,), 1) is None
    finally:
        _ssrf_policy.apply_server_policy({"block_private_networks": False})
    assert no_connections == [], no_connections


# --- Many HTTPS checks on a slow domain leave a healthy lookup working -----------


async def _healthy_after(
    dns: Dns, monkeypatch: pytest.MonkeyPatch, scheme: str, n: int
) -> Any:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.5)
    checks = [
        HTTPCheck(_cfg("http", f"{scheme}://slowcert.gated.test/p{i}", timeout=1))
        for i in range(n)
    ]
    await asyncio.gather(*(c.run() for c in checks), return_exceptions=True)
    # Every check has finished. Now a healthy ping's lookup:
    try:
        return await _ssrf_policy.lookup_allowed_addresses("healthy.test")
    except _lookups.LookupTimeoutError as e:
        return e


async def test_https_checks_on_slow_dns_leave_a_healthy_lookup_working(
    dns: Dns, monkeypatch: pytest.MonkeyPatch, no_connections: list[Any]
) -> None:
    got = await _healthy_after(dns, monkeypatch, "https", _lookups.LOOKUP_WORKERS)
    queued = _lookups.TARGET_LOOKUPS._work_queue.qsize()
    dns.gate.set()
    await _drained(30)
    await _close_clients()
    assert got == ["8.8.8.8"], f"healthy lookup got {got!r}; queued={queued}"


async def test_http_checks_on_slow_dns_leave_a_healthy_lookup_working(
    dns: Dns, monkeypatch: pytest.MonkeyPatch, no_connections: list[Any]
) -> None:
    got = await _healthy_after(dns, monkeypatch, "http", _lookups.LOOKUP_WORKERS)
    dns.gate.set()
    await _drained(30)
    await _close_clients()
    assert got == ["8.8.8.8"], got


# --- A lookup timeout is named and not retried, by http and tcp alike ----------


async def test_an_http_lookup_timeout_is_named_and_not_retried(
    dns: Dns, monkeypatch: pytest.MonkeyPatch, no_connections: list[Any]
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.3)
    check = HTTPCheck(_cfg("http", "http://slowhttp.gated.test/", timeout=5, retries=3))
    begun = time.monotonic()
    result, _ = await _executor()._run(check, timeout=10, longest=0)
    took = time.monotonic() - begun
    dns.gate.set()
    await _drained()
    await _close_clients()
    assert took < 0.6 and "Could not resolve host" in result["error"], (
        took,
        result["error"],
    )


async def test_a_tcp_lookup_timeout_is_named_and_not_retried_again(
    dns: Dns, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.3)
    check = TCPCheck(_cfg("tcp", "slowtcp.gated.test", port=443, timeout=5, retries=3))
    begun = time.monotonic()
    result, _ = await _executor()._run(check, timeout=10, longest=0)
    took = time.monotonic() - begun
    dns.gate.set()
    await _drained()
    assert took < 0.6 and "Could not resolve host" in result["error"], (
        took,
        result["error"],
    )


# --- Every lookup timeout names its host ----------------------------------------


async def test_a_dns_lookup_timeout_names_the_host(
    dns: Dns, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.3)
    check = DNSCheck(
        _cfg(
            "dns", "example.org", nameserver="ns.gated.test", record_type="A", timeout=2
        )
    )
    result, _ = await _executor()._run(check, timeout=10, longest=0)
    dns.gate.set()
    await _drained()
    assert "ns.gated.test" in result["error"], result["error"]


async def test_a_ping_lookup_timeout_names_the_host(
    dns: Dns, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.3)
    result, _ = await _executor()._run(
        PingCheck(_cfg("ping", "pinghost.gated.test")), timeout=10, longest=0
    )
    dns.gate.set()
    await _drained()
    assert "pinghost.gated.test" in result["error"], result["error"]


# --- A failed request opens no certificate connection ---------------------------


async def test_a_failed_request_opens_no_certificate_connection(
    dns: Dns, no_connections: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # healthy.test resolves at once to a public address; the request's connect is
    # refused (recorded); the probe should not then dial the same host itself.
    result = await HTTPCheck(_cfg("http", "https://healthy.test/", timeout=2)).run()
    await _drained()
    await _close_clients()
    assert result["success"] is False
    assert ("request", "8.8.8.8", 443) in no_connections, no_connections
    assert ("probe", "8.8.8.8", 443) not in no_connections, no_connections
