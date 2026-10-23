"""The network_discover job reports the agent's networks as scan targets: its
interfaces (loopback, link-local and Docker bridges left out), the default
gateway (looked through a Docker bridge with a TTL-2 ping when it is one), the
ARP neighbours, and a suggested range per interface.

The commands it runs (`ip`, `route`, `ping`) and the /proc files it reads are
stubbed with real output shapes, so the parsing is what is under test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from app.shared.jobs import network_discover
from app.shared.jobs.network_discover import NetworkDiscoverJob

pytestmark = pytest.mark.unit

IP_ADDR = b"""1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
2: eth0    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
4: wlan0    inet 169.254.3.4/16 scope link wlan0
5: corp0    inet 10.20.30.40/16 brd 10.20.255.255 scope global corp0
6: odd0    inet garbage/xx scope global odd0
7: short
"""

PROC_ROUTE = """Iface\tDestination\tGateway \tFlags
eth0\t00000000\t0101A8C0\t0003
eth0\t0001A8C0\t00000000\t0001
"""

PROC_ARP = """IP address       HW type     Flags       HW address
192.168.1.1      0x1         0x2         aa:bb:cc:dd:ee:ff
0.0.0.0          0x1         0x0         00:00:00:00:00:00

192.168.1.50     0x1         0x2         11:22:33:44:55:66
"""

TTL_PING = b"""PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
From 192.168.1.1 icmp_seq=1 Time to live exceeded
"""

Output = tuple[int | None, bytes | None, bytes | None]


@dataclass
class Host:
    """What the stubbed host answers: command output by command name, file
    contents by path, and every command the job ran."""

    responders: dict[str, Callable[[tuple[str, ...]], Output]] = field(
        default_factory=dict
    )
    files: dict[str, str] = field(default_factory=dict)
    ran: list[tuple[str, ...]] = field(default_factory=list)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Host:
    stub = Host()

    async def _run(*args: str, **_: object) -> Output:
        stub.ran.append(args)
        responder = stub.responders.get(args[0])
        if responder is None:
            raise OSError(f"{args[0]}: not found")
        return responder(args)

    class _Path:
        def __init__(self, path: str) -> None:
            self.path = path

        def read_text(self) -> str:
            if self.path not in stub.files:
                raise FileNotFoundError(self.path)
            return stub.files[self.path]

    monkeypatch.setattr(network_discover, "run_subprocess_safely", _run)
    monkeypatch.setattr(network_discover, "Path", _Path)
    return stub


def _ip(route: Output) -> Callable[[tuple[str, ...]], Output]:
    """`ip route …` answers `route`; `ip addr …` answers IP_ADDR."""
    return lambda args: route if args[1] == "route" else (0, IP_ADDR, b"")


async def test_networks_are_reported_with_gateway_and_neighbours(host: Host) -> None:
    host.responders["ip"] = _ip((0, b"default via 192.168.1.1 dev eth0", b""))
    host.files["/proc/net/arp"] = PROC_ARP

    outcome = await NetworkDiscoverJob("job-1", {}).run()

    assert outcome["status"] == "completed", outcome
    result = outcome["result"]
    assert result["default_gateway"] == "192.168.1.1"
    assert result["arp_neighbors"] == ["192.168.1.1", "192.168.1.50"]
    # the network with the gateway first; loopback, link-local, the Docker
    # bridge and the unparsable line are left out
    assert [i["name"] for i in result["interfaces"]] == ["eth0", "corp0"]
    eth0, corp0 = result["interfaces"]
    assert eth0["has_gateway"] is True
    assert eth0["suggested_scan"] == "192.168.1.0/24"
    assert eth0["netmask"] == "255.255.255.0"
    # a /16 suggests the /24 the agent sits in
    assert corp0["suggested_scan"] == "10.20.30.0/24"
    assert corp0["network_size"] == 65536
    assert "real_gateway" not in result


async def test_the_gateway_falls_back_to_proc_net_route(host: Host) -> None:
    host.responders["ip"] = _ip((1, b"", b""))
    host.responders["route"] = lambda args: (1, b"", b"")
    host.files["/proc/net/route"] = PROC_ROUTE

    result = await NetworkDiscoverJob("j", {}).execute()

    assert result["default_gateway"] == "192.168.1.1"


async def test_the_bsd_route_command_is_tried_when_ip_is_missing(host: Host) -> None:
    host.responders["route"] = lambda args: (
        0,
        b"   route to: default\n    gateway: 10.0.0.1\n",
        b"",
    )

    assert await NetworkDiscoverJob("j", {})._get_default_gateway() == "10.0.0.1"


async def test_a_docker_gateway_is_looked_through_with_a_ttl_ping(host: Host) -> None:
    host.responders["ip"] = _ip((0, b"default via 172.17.0.1 dev eth0", b""))
    host.responders["ping"] = lambda args: (1, TTL_PING, b"")

    result = await NetworkDiscoverJob("j", {}).execute()

    assert result["docker_gateway"] == "172.17.0.1"
    assert result["real_gateway"] == "192.168.1.1"
    assert result["default_gateway"] == "192.168.1.1"
    ping = next(a for a in host.ran if a[0] == "ping")
    assert ping == ("ping", "-c", "1", "-t", "2", "8.8.8.8")


async def test_a_ttl_ping_without_a_hop_finds_no_gateway(host: Host) -> None:
    host.responders["ping"] = lambda args: (
        0,
        b"From not-an-ip icmp_seq=1 Time to live exceeded\n",
        b"",
    )

    assert await NetworkDiscoverJob("j", {})._discover_real_gateway_via_ttl() is None


async def test_without_the_ip_command_the_host_address_is_used(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_discover.socket, "gethostname", lambda: "agent-host")
    monkeypatch.setattr(
        network_discover.socket, "gethostbyname", lambda name: "10.9.8.7"
    )

    result = await NetworkDiscoverJob("j", {}).execute()

    assert result["hostname"] == "agent-host"
    assert result["default_gateway"] is None
    (iface,) = result["interfaces"]
    assert (iface["name"], iface["cidr"]) == ("default", "10.9.8.0/24")


async def test_the_fallback_reports_nothing_when_the_name_does_not_resolve(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(name: str) -> str:
        raise OSError("no address")

    monkeypatch.setattr(network_discover.socket, "gethostbyname", _fail)

    assert await NetworkDiscoverJob("j", {})._get_interfaces_fallback() == []


@pytest.mark.parametrize(
    ("cidr", "bridge"),
    [
        ("172.17.0.0/16", True),
        ("172.16.0.0/12", True),
        ("172.31.0.1/32", True),
        ("172.32.0.0/16", False),
        ("192.168.1.0/24", False),
        ("fd00::/64", False),
        ("nonsense", False),
    ],
)
def test_docker_bridge_ranges(cidr: str, bridge: bool) -> None:
    assert NetworkDiscoverJob("j", {})._is_docker_bridge_network(cidr) is bridge


def test_containerised_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    job = NetworkDiscoverJob("j", {})
    monkeypatch.setattr(network_discover.os.path, "exists", lambda p: True)
    assert job._is_containerized() is True

    monkeypatch.setattr(network_discover.os.path, "exists", lambda p: False)

    def _open(path: str, *args: object, **kwargs: object):
        raise OSError(path)

    monkeypatch.setattr("builtins.open", _open)
    assert job._is_containerized() is False
