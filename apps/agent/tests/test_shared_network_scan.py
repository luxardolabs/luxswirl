"""The network_scan job: ping every host of a subnet, and for each that answers,
resolve its name and probe the requested TCP ports.

Ping is the only part stubbed (the image may not grant ICMP); the port probe
connects to real sockets on 127.0.0.1, and the job runs through BaseJob.run, the
way the agent's job processor drives it.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.shared.jobs import network_scan
from app.shared.jobs.network_scan import NetworkScanJob, NetworkScanParams
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def open_port() -> AsyncIterator[int]:
    """A TCP port on 127.0.0.1 that accepts connections."""

    async def _accept(reader, writer) -> None:
        writer.close()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _closed_port() -> int:
    """A port nothing listens on (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Stub ping: 127.0.0.1 answers, every other address does not."""
    commands: list[tuple[str, ...]] = []

    async def _run(*args: str, **_: object) -> int:
        commands.append(args)
        return 0 if args[-1] == "127.0.0.1" else 1

    monkeypatch.setattr(network_scan, "run_subprocess_no_output", _run)
    return commands


async def test_a_responding_host_is_reported_with_its_open_ports(
    pings: list[tuple[str, ...]], open_port: int
) -> None:
    closed = _closed_port()
    job = NetworkScanJob(
        "job-1",
        {"subnet": "127.0.0.0/30", "ports": [open_port, closed], "timeout": 2},
    )

    outcome = await job.run()

    assert outcome["status"] == "completed", outcome
    result = outcome["result"]
    assert result["hosts_scanned"] == 2  # .1 and .2
    assert result["hosts_responding"] == 1
    (host,) = result["discovered_hosts"]
    assert host["ip"] == "127.0.0.1"
    assert host["responds_to_ping"] is True
    assert host["open_ports"] == [open_port]
    assert sorted(cmd[-1] for cmd in pings) == ["127.0.0.1", "127.0.0.2"]
    assert pings[0][:5] == ("ping", "-c", "1", "-W", "2")
    assert job.duration is not None


async def test_an_empty_port_list_pings_only(pings) -> None:
    result = await NetworkScanJob(
        "j", {"subnet": "127.0.0.1/32", "ports": []}
    ).execute()

    assert result["discovered_hosts"][0]["open_ports"] == []


async def test_a_large_subnet_is_scanned_in_slash_24_chunks(pings) -> None:
    result = await NetworkScanJob(
        "j", {"subnet": "127.0.0.0/23", "ports": [], "max_concurrent": 500}
    ).execute()

    assert result["chunks_scanned"] == 2
    assert [c["subnet"] for c in result["chunk_metrics"]] == [
        "127.0.0.0/24",
        "127.0.1.0/24",
    ]
    assert result["hosts_scanned"] == 510
    assert [h["ip"] for h in result["discovered_hosts"]] == ["127.0.0.1"]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"subnet": "not-a-subnet"},
        {"subnet": "10.0.0.0/15"},  # 131072 addresses
        {"subnet": "10.0.0.0/24", "ports": [0]},
        {"subnet": "10.0.0.0/24", "ports": [65536]},
        {"subnet": "10.0.0.0/24", "timeout": 0},
        {"subnet": "10.0.0.0/24", "max_concurrent": 1},
    ],
)
async def test_invalid_parameters_fail_the_job_without_scanning(params, pings) -> None:
    outcome = await NetworkScanJob("j", params).run()

    assert outcome["status"] == "failed"
    assert outcome["result"] is None
    assert "Invalid parameters" in outcome["error"]
    assert pings == []


def test_the_parameter_schema_defaults() -> None:
    params = NetworkScanParams(subnet="192.168.1.0/24")

    assert (params.timeout, params.max_concurrent) == (10, 100)
    assert 22 in params.ports
    with pytest.raises(ValidationError):
        NetworkScanParams(subnet="192.168.1.0/24", ports=[22, 70000])


@pytest.mark.parametrize("error", [TimeoutError, OSError])
async def test_a_ping_that_errors_counts_as_no_answer(
    error, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run(*args: str, **_: object) -> int:
        raise error

    monkeypatch.setattr(network_scan, "run_subprocess_no_output", _run)

    assert await NetworkScanJob("j", {})._ping_host("127.0.0.1", 1) is False


async def test_windows_pings_with_its_own_flags(
    monkeypatch: pytest.MonkeyPatch, pings
) -> None:
    monkeypatch.setattr(network_scan.platform, "system", lambda: "Windows")

    await NetworkScanJob("j", {})._ping_host("127.0.0.1", 3)

    assert pings == [("ping", "-n", "1", "-w", "3000", "127.0.0.1")]


@pytest.mark.parametrize(
    "error",
    [
        socket.herror("no name"),
        socket.gaierror("no name"),
        OSError("down"),
        RuntimeError("unexpected"),
    ],
)
async def test_a_failed_reverse_lookup_gives_no_hostname(
    error, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _lookup(ip: str):
        raise error

    monkeypatch.setattr(network_scan.socket, "gethostbyaddr", _lookup)

    assert await NetworkScanJob("j", {})._resolve_hostname("127.0.0.1") is None


async def test_a_slow_reverse_lookup_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _slow(ip: str):
        time.sleep(2)
        return ("late", [], [ip])

    monkeypatch.setattr(network_scan.socket, "gethostbyaddr", _slow)

    assert await NetworkScanJob("j", {})._resolve_hostname("127.0.0.1", 1) is None


async def test_a_resolved_name_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        network_scan.socket, "gethostbyaddr", lambda ip: ("printer.lan", [], [ip])
    )

    assert await NetworkScanJob("j", {})._resolve_hostname("10.0.0.9") == "printer.lan"


def test_the_job_names_itself() -> None:
    assert "network_scan" in repr(NetworkScanJob("job-9", {}))


@pytest.mark.parametrize(
    "subnet",
    ["169.254.169.254/32", "169.254.0.0/24", "169.254.0.0/16", "fe80::/120"],
)
def test_a_scan_never_reaches_the_cloud_metadata_range(subnet: str) -> None:
    """The metadata service serves cloud credentials; no sweep has a use for it."""
    with pytest.raises(ValidationError, match="cloud metadata"):
        NetworkScanParams(subnet=subnet)


@pytest.mark.parametrize(
    "subnet", ["192.168.1.0/24", "10.20.0.0/16", "127.0.0.1/32", "169.253.255.0/24"]
)
def test_a_scan_of_a_private_network_is_allowed(subnet: str) -> None:
    """Sweeping the LAN is what scans are for, whatever block_private_networks says."""
    assert NetworkScanParams(subnet=subnet).subnet == subnet


async def test_a_metadata_scan_reaching_the_agent_is_refused_before_any_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []

    async def no_probe(self, ip: str, *_: object, **__: object) -> None:
        probed.append(ip)

    monkeypatch.setattr(NetworkScanJob, "_scan_single_host", no_probe)
    job = NetworkScanJob(job_id="j1", params={"subnet": "169.254.169.254/32"})

    with pytest.raises(ValueError, match="cloud metadata"):
        await job.execute()
    assert probed == []


@pytest.mark.parametrize(
    "subnet",
    [
        "::ffff:169.254.169.254/128",  # IPv4-mapped: a dual-stack socket reaches it
        "::ffff:169.254.169.0/120",
        "::ffff:a9fe:0/112",  # the whole mapped 169.254.0.0/16
        "64:ff9b::a9fe:a9fe/128",  # NAT64
        "2002:a9fe:a9fe::/112",  # 6to4
    ],
)
def test_an_ipv6_form_carrying_a_metadata_address_is_refused(subnet: str) -> None:
    with pytest.raises(ValidationError, match="cloud metadata"):
        NetworkScanParams(subnet=subnet)


@pytest.mark.parametrize(
    "subnet", ["::ffff:192.168.1.0/120", "64:ff9b::a00:0/120", "2002:c0a8:100::/120"]
)
def test_an_ipv6_form_carrying_a_lan_address_is_allowed(subnet: str) -> None:
    assert NetworkScanParams(subnet=subnet).subnet == subnet


async def test_a_mapped_metadata_scan_is_refused_before_any_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []

    async def no_probe(self, ip: str, *_: object, **__: object) -> None:
        probed.append(ip)

    monkeypatch.setattr(NetworkScanJob, "_scan_single_host", no_probe)
    job = NetworkScanJob(job_id="j1", params={"subnet": "::ffff:169.254.169.254/128"})

    with pytest.raises(ValueError, match="cloud metadata"):
        await job.execute()
    assert probed == []
