"""SSRF protection — block check targets that resolve into dangerous IP ranges.

Shared by the server (validates at check create/update time) and the agent
(re-validates at *fetch* time). Fetch-time validation is what closes the real
bypasses: a host that resolved safe at create can resolve to the cloud-metadata
IP at fetch (DNS rebinding), or an HTTP target can redirect to it.

The cloud-metadata / link-local range is blocked by default because it serves the
instance's IAM credentials (the classic SSRF prize — see the 2019 Capital One
breach). RFC-1918 private networks are *allowed* by default, since monitoring LAN
hosts is the intended use; the server can opt into blocking them via settings.

A check that opens its own connection resolves the name once
(:func:`allowed_addresses`), drops every address in a blocked range, and connects
to one that is left: ping, tcp and the http/json checks, whose TLS still verifies
the name. A name is refused only when it resolves and none of its addresses is
allowed, so a host that also answers with its IPv6 link-local address is reached
on its other one, and nothing resolves the name again between the check and the
socket. The database and DNS checks hand the name to a driver that resolves it
itself: :func:`assert_target_allowed` refuses those when ANY address is blocked, and
a database URL whose hosts could be read two ways, or that names more than
`MAX_TARGET_HOSTS` hosts. A unix socket a PostgreSQL URL names is this host's own,
so it is refused with private networks.

An IPv6 address that carries an IPv4 one (:data:`_IPV4_CARRIERS`) is judged as
that IPv4 address too: a translator on the network takes it there.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

from .logger import get_logger

logger = get_logger("luxswirl.security.ssrf")

# Cloud metadata + link-local — dangerous in any cloud/container environment.
CLOUD_METADATA_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure IMDS + link-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS over IPv6
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
    ipaddress.ip_network("192.0.0.192/32"),  # Oracle Cloud (Classic) metadata
    ipaddress.ip_network("168.63.129.16/32"),  # Azure WireServer (VM agent channel)
    ipaddress.ip_network("fd20:ce::254/128"),  # Google Compute Engine metadata, IPv6
    ipaddress.ip_network("fd00:a9fe:a9fe::1/128"),  # Akamai / Linode Metadata API, IPv6
]

# Private and internal networks — where most self-hosted targets live, and the
# host's own services (loopback; 0.0.0.0 reaches it on Linux).
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("0.0.0.0/8"),  # unspecified / "this host"
    ipaddress.ip_network("::/128"),  # IPv6 unspecified
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT shared address space
    # Local-use NAT64 (RFC 8215): the local network's own translation, whose
    # IPv4 address no reader can be sure of.
    ipaddress.ip_network("64:ff9b:1::/48"),
]

# The most hosts one address may name: each is looked up in turn.
MAX_TARGET_HOSTS = 16


class SsrfBlockedError(ValueError):
    """Raised when a target resolves into a blocked IP range."""


class UnallowableTargetError(SsrfBlockedError):
    """Raised for a target no setting allows, since the agent refuses it whatever the
    settings are. Its text never quotes the target, which can hold a password."""


class UnplainDatabaseUrlError(UnallowableTargetError):
    """Raised for a database URL a driver could read another way."""


class TooManyHostsError(UnallowableTargetError):
    """Raised for an address naming more than `MAX_TARGET_HOSTS` hosts."""


def extract_host(target: str) -> str | None:
    """Extract the hostname from a URL or ``host[:port]`` target string."""
    if "://" in target:
        try:
            return urlparse(target).hostname
        except ValueError:
            return None
    # host:port, [v6] or [v6]:port; a bare IPv6 (several colons) is left alone.
    return _strip_port(target.strip()) or None


# libpq/asyncpg URLs name several hosts: a comma list in the netloc, and the
# `host` / `hostaddr` query parameters.
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})
# One entry of a PostgreSQL host list: a name or a bracketed IPv6 literal, and a port.
_POSTGRES_HOST_ENTRY = re.compile(
    r"(?:\[(?P<literal>[0-9A-Fa-f:.]+)\]|[\w.-]+)?(?::\d*)?"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s")
# A database URL that names no host connects to the driver's default: localhost.
_DATABASE_SCHEMES = _POSTGRES_SCHEMES | {"mysql", "mariadb"}
_DEFAULT_DATABASE_HOST = "localhost"


def _strip_port(host: str) -> str:
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _reads_as_database_url(target: str) -> bool:
    """Whether a database driver takes `target` for its URL. A driver goes by the
    parsed scheme, so `postgres:?host=db` is one with no `//`."""
    try:
        scheme = urlparse(target).scheme.lower().split("+", 1)[0]
    except ValueError:
        return False
    return scheme in _DATABASE_SCHEMES


def _is_plain_postgres_host(entry: str) -> bool:
    """Whether one entry of a host list says one thing to any reader: a name or a
    bracketed IPv6 literal, with no scope, and an optional port."""
    match = _POSTGRES_HOST_ENTRY.fullmatch(entry)
    if match is None:
        return False
    literal = match["literal"]
    if literal is None:
        return True
    try:
        ipaddress.IPv6Address(literal)
    except ValueError:
        return False
    return True


def _is_plain_postgres_url(target: str) -> bool:
    parsed = urlparse(target)
    if _WHITESPACE.search(target) or parsed.netloc.count("@") > 1:
        return False
    query = parse_qs(parsed.query)
    if "service" in query:
        return False
    for entry in parsed.netloc.rpartition("@")[2].split(","):
        if not (unquote(entry).startswith("/") or _is_plain_postgres_host(entry)):
            return False
    for value in query.get("host", []):
        for entry in value.split(","):
            if not (entry.startswith("/") or _is_plain_postgres_host(entry)):
                return False
    for value in query.get("hostaddr", []):
        for entry in value.split(","):
            try:
                ipaddress.ip_address(entry)
            except ValueError:
                return False
            if "%" in entry:
                return False
    return True


def _require_plain_database_url(target: str) -> None:
    """Refuse a database URL whose hosts a driver could read another way.

    A driver's parser and this module's cannot be kept the same on every odd form, so
    the odd forms are not read at all: a URL has `//` after its scheme and holds no
    control character. A PostgreSQL URL also holds no whitespace, names each host
    plainly, has one `@` before its hosts, and names no `service`, which reads a host
    from a file. A MySQL URL's host is read one way by both, so its password stays as
    written. The refusal never quotes the URL, which holds the password.

    Raises:
        UnplainDatabaseUrlError: `target` is not a plain database URL.
    """
    plain = (
        target.partition(":")[2].startswith("//") and _CONTROL.search(target) is None
    )
    if plain and urlparse(target).scheme.lower().split("+", 1)[0] in _POSTGRES_SCHEMES:
        plain = _is_plain_postgres_url(target)
    if not plain:
        raise UnplainDatabaseUrlError(
            "This database URL is blocked: its hosts cannot be read plainly. Write it "
            "as scheme://user:password@host:port/database. In a PostgreSQL password, "
            "write `@` as %40 and a space as %20."
        )


def _postgres_entry_host(entry: str) -> str:
    """The host in one entry of a PostgreSQL host list."""
    text = unquote(entry).strip()
    if text.startswith("["):
        return text[1 : text.find("]")] if "]" in text else text[1:]
    return text.partition(":")[0]


def url_hosts(target: str) -> list[str]:
    """Every host the URL `target` connects to: one, or each host a PostgreSQL URL names
    (a unix-socket path is not a network host). A driver may ignore `hostaddr`, so it
    never stands in for the default host."""
    try:
        parsed = urlparse(target)
    except ValueError:
        return []
    scheme = parsed.scheme.lower().split("+", 1)[0]
    if scheme not in _POSTGRES_SCHEMES:
        if parsed.hostname:
            return [parsed.hostname]
        return [_DEFAULT_DATABASE_HOST] if scheme in _DATABASE_SCHEMES else []
    query = parse_qs(parsed.query)
    entries = [
        *parsed.netloc.rpartition("@")[2].split(","),
        *(entry for value in query.get("host", []) for entry in value.split(",")),
    ]
    named = [_postgres_entry_host(entry) for entry in entries]
    hosts = [host for host in named if host and not host.startswith("/")]
    if not hosts and not any(host.startswith("/") for host in named):
        hosts.append(_DEFAULT_DATABASE_HOST)
    for value in query.get("hostaddr", []):
        hosts.extend(value.split(","))
    return list(dict.fromkeys(host for host in hosts if host))


def unix_socket_paths(target: str) -> list[str]:
    """The unix-socket paths a PostgreSQL URL `target` names: each entry of its host
    list or `host` parameter that is a path. A driver connects to this host there."""
    try:
        parsed = urlparse(target)
    except ValueError:
        return []
    if parsed.scheme.lower().split("+", 1)[0] not in _POSTGRES_SCHEMES:
        return []
    entries = [
        *parsed.netloc.rpartition("@")[2].split(","),
        *(
            entry
            for value in parse_qs(parsed.query).get("host", [])
            for entry in value.split(",")
        ),
    ]
    named = (_postgres_entry_host(entry) for entry in entries)
    return [host for host in named if host.startswith("/")]


def extract_hosts(target: str, *, database: bool = False) -> list[str]:
    """Every host ``target`` connects to, for a URL or ``host[:port]``.

    `database` says a database driver will read it, so a target with a database scheme
    is its URL (`postgres:5432` is a TCP check's host and port, and only the caller knows
    which).

    Raises:
        UnplainDatabaseUrlError: A driver would read it, and it is not a plain URL.
        TooManyHostsError: It names more than `MAX_TARGET_HOSTS` hosts.
    """
    if database and _reads_as_database_url(target):
        _require_plain_database_url(target)
        hosts = url_hosts(target)
    elif "://" in target:
        hosts = url_hosts(target)
    else:
        host = extract_host(target)
        hosts = [host] if host else []
    if len(hosts) > MAX_TARGET_HOSTS:
        raise TooManyHostsError(
            f"This address names {len(hosts)} hosts: at most {MAX_TARGET_HOSTS} "
            "are allowed."
        )
    return hosts


# How an IPv6 address carries an IPv4 one: a reader takes the IPv4 address from the
# IPv6 address's integer, its bits in order.
_Reader = Callable[[int], int]


def _bits(shift: int, width: int) -> _Reader:
    """The `width` bits that end `shift` bits from the address's low end."""

    def read(value: int) -> int:
        return (value >> shift) & ((1 << width) - 1)

    return read


def _split(high: _Reader, high_width: int, low: _Reader) -> _Reader:
    """Two runs of bits read as one (RFC 6052 skips bits 64-71 between them)."""

    def read(value: int) -> int:
        return (high(value) << (32 - high_width)) | low(value)

    return read


def _inverted(reader: _Reader) -> _Reader:
    def read(value: int) -> int:
        return ~reader(value) & 0xFFFFFFFF

    return read


_LOW32 = _bits(0, 32)

# IPv6 prefixes whose addresses carry an IPv4 one a socket can reach through a
# translator, and each way of reading it. A reader takes its bits in address order,
# so over a CIDR block its readings run from its reading of the block's first
# address to its reading of the last (the other way round for an inverted one).
_IPV4_CARRIERS: tuple[tuple[ipaddress.IPv6Network, tuple[_Reader, ...]], ...] = (
    (ipaddress.IPv6Network("::ffff:0:0/96"), (_LOW32,)),  # IPv4-mapped
    (ipaddress.IPv6Network("::/96"), (_LOW32,)),  # IPv4-compatible (deprecated)
    (ipaddress.IPv6Network("::ffff:0:0:0/96"), (_LOW32,)),  # SIIT IPv4-translated
    (ipaddress.IPv6Network("2002::/16"), (_bits(80, 32),)),  # 6to4
    (ipaddress.IPv6Network("64:ff9b::/96"), (_LOW32,)),  # NAT64 (RFC 6052)
    # Local-use NAT64 (RFC 8215), under each operator prefix length RFC 6052 allows:
    # /48, /56, /64 and /96.
    (
        ipaddress.IPv6Network("64:ff9b:1::/48"),
        (
            _split(_bits(64, 16), 16, _bits(40, 16)),
            _split(_bits(64, 8), 8, _bits(32, 24)),
            _bits(24, 32),
            _LOW32,
        ),
    ),
    # Teredo: its server, and its client (stored inverted).
    (ipaddress.IPv6Network("2001::/32"), (_bits(64, 32), _inverted(_LOW32))),
)


def _carried_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address an IPv6 one carries (:data:`_IPV4_CARRIERS`)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    value = int(ip)
    return [
        ipaddress.IPv4Address(read(value))
        for carrier, readers in _IPV4_CARRIERS
        if ip in carrier
        for read in readers
    ]


def network_cloud_metadata_overlap(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """The cloud-metadata range `network` reaches, or None.

    Directly, or through the part of an IPv6 network that carries IPv4
    addresses (:data:`_IPV4_CARRIERS`): over such a part, each way of reading
    the IPv4 address gives one contiguous range, read from its first and last
    address.
    """
    for blocked in CLOUD_METADATA_NETWORKS:
        if network.version == blocked.version and network.overlaps(blocked):
            return blocked
    if not isinstance(network, ipaddress.IPv6Network):
        return None
    for carrier, readers in _IPV4_CARRIERS:
        if not network.overlaps(carrier):
            continue
        first = max(int(network.network_address), int(carrier.network_address))
        last = min(int(network.broadcast_address), int(carrier.broadcast_address))
        for read in readers:
            low, high = sorted((read(first), read(last)))
            for blocked in CLOUD_METADATA_NETWORKS:
                if (
                    blocked.version == 4
                    and low <= int(blocked.broadcast_address)
                    and high >= int(blocked.network_address)
                ):
                    return blocked
    return None


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname (or pass a literal IP through) to a list of IP strings.

    Returns ``[]`` when resolution fails — the caller's own connection will then
    fail, so there is nothing to forge a request to.
    """
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        # In the resolver's order (its preference), each address once.
        return list(
            dict.fromkeys(info[4][0] for info in infos if isinstance(info[4][0], str))
        )
    except socket.gaierror, OSError:
        return []


def blocked_range(
    ip_str: str,
    *,
    block_cloud_metadata: bool = True,
    block_private_networks: bool = False,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str] | None:
    """The blocked range ``ip_str`` falls in and its label, or None when allowed."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    candidates = [ip, *_carried_ipv4(ip)]
    ranges: list[tuple[list[ipaddress.IPv4Network | ipaddress.IPv6Network], str]] = []
    if block_cloud_metadata:
        ranges.append((CLOUD_METADATA_NETWORKS, "cloud metadata / link-local"))
    if block_private_networks:
        ranges.append((PRIVATE_NETWORKS, "private network"))
    for networks, label in ranges:
        matched = next((net for net in networks for c in candidates if c in net), None)
        if matched is not None:
            return matched, label
    return None


def assert_ip_allowed(
    ip_str: str,
    *,
    block_cloud_metadata: bool = True,
    block_private_networks: bool = False,
) -> None:
    """Raise :class:`SsrfBlockedError` if ``ip_str`` is in a blocked range.

    Use this when you already hold the resolved IP you're about to connect to
    (e.g. TCP) — it validates the exact pinned IP, with no re-resolution gap.
    """
    blocked = blocked_range(
        ip_str,
        block_cloud_metadata=block_cloud_metadata,
        block_private_networks=block_private_networks,
    )
    if blocked is None:
        return
    matched, label = blocked
    # Without its scope: the text after `%` is the submitter's own, never an address.
    address = ip_str.partition("%")[0]
    logger.warning(
        "SSRF protection: blocked IP",
        extra={"resolved_ip": address, "range": str(matched)},
    )
    raise SsrfBlockedError(f"{address} is in the blocked {label} range ({matched}).")


def allowed_addresses(
    host: str,
    *,
    block_cloud_metadata: bool = True,
    block_private_networks: bool = False,
) -> list[str]:
    """The addresses ``host`` resolves to that the policy allows, in the
    resolver's order: the ones to connect to. Empty when it does not resolve.

    Raises:
        SsrfBlockedError: It resolves, and every address is in a blocked range.
    """
    addresses = resolve_host(host)
    allowed = [
        ip
        for ip in addresses
        if blocked_range(
            ip,
            block_cloud_metadata=block_cloud_metadata,
            block_private_networks=block_private_networks,
        )
        is None
    ]
    if addresses and not allowed:
        assert_ip_allowed(
            addresses[0],
            block_cloud_metadata=block_cloud_metadata,
            block_private_networks=block_private_networks,
        )
    return allowed


def assert_target_allowed(
    target: str,
    *,
    block_cloud_metadata: bool = True,
    block_private_networks: bool = False,
    database: bool = False,
) -> None:
    """Resolve ``target`` and raise :class:`SsrfBlockedError` if any resolved IP is blocked.

    Call this immediately before connecting (fetch time) so it also defends against
    DNS-rebinding and redirect bypasses — not just the value configured at create time.
    `database`: a database driver will read it (:func:`extract_hosts`).
    """
    hosts = extract_hosts(target, database=database)
    if not block_cloud_metadata and not block_private_networks:
        return
    if block_private_networks and database and unix_socket_paths(target):
        logger.warning("SSRF protection: blocked a unix socket")
        raise SsrfBlockedError(
            "A unix socket is in the blocked private network range: it reaches this "
            "host's own services."
        )
    for host in hosts:
        for ip_str in resolve_host(host):
            assert_ip_allowed(
                ip_str,
                block_cloud_metadata=block_cloud_metadata,
                block_private_networks=block_private_networks,
            )
