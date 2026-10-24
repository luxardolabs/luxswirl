"""SSRF protection — block check targets that resolve into dangerous IP ranges.

Shared by the server (validates at check create/update time) and the agent
(re-validates at *fetch* time). Fetch-time validation is what closes the real
bypasses: a host that resolved safe at create can resolve to the cloud-metadata
IP at fetch (DNS rebinding), or an HTTP target can redirect to it.

The cloud-metadata / link-local range is blocked by default because it serves the
instance's IAM credentials (the classic SSRF prize — see the 2019 Capital One
breach). RFC-1918 private networks are *allowed* by default, since monitoring LAN
hosts is the intended use; the server can opt into blocking them via settings.

Residual: this validates the resolved IPs immediately before connecting, which
closes the create→fetch window. A sub-millisecond TTL-0 rebinding between this
resolve and the socket's own resolve is not closed here (would require pinning the
validated IP through TLS/SNI) — documented as a known further-hardening item.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import parse_qs, unquote, urlparse

from .logger import get_logger

logger = get_logger("luxswirl.security.ssrf")

# Cloud metadata + link-local — dangerous in any cloud/container environment.
CLOUD_METADATA_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure IMDS + link-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
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
]


class SsrfBlockedError(ValueError):
    """Raised when a target resolves into a blocked IP range."""


def extract_host(target: str) -> str | None:
    """Extract the hostname from a URL or ``host[:port]`` target string."""
    if "://" in target:
        try:
            return urlparse(target).hostname
        except ValueError:
            return None
    # host:port — but leave bare IPv6 (multiple colons) alone.
    if target.count(":") == 1:
        return target.rsplit(":", 1)[0]
    return target or None


# libpq/asyncpg URLs name several hosts: a comma list in the netloc, and the
# `host` / `hostaddr` query parameters.
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})

# IPv6 forms that carry an IPv4 address a socket reaches (RFC 6052 NAT64).
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _strip_port(host: str) -> str:
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def extract_hosts(target: str) -> list[str]:
    """Every host ``target`` connects to: one for a URL or ``host[:port]``, and
    each host a PostgreSQL URL names (a unix-socket path is not a network host)."""
    if "://" not in target:
        host = extract_host(target)
        return [host] if host else []
    try:
        parsed = urlparse(target)
    except ValueError:
        return []
    if parsed.scheme.lower().split("+", 1)[0] not in _POSTGRES_SCHEMES:
        return [parsed.hostname] if parsed.hostname else []
    named = parsed.netloc.rpartition("@")[2].split(",")
    query = parse_qs(parsed.query)
    for key in ("host", "hostaddr"):
        for value in query.get(key, []):
            named.extend(value.split(","))
    hosts = (_strip_port(unquote(h).strip()) for h in named)
    return [h for h in hosts if h and not h.startswith("/")]


def _embedded_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one carries (mapped, 6to4, NAT64), if any."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
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
        return list({info[4][0] for info in infos if isinstance(info[4][0], str)})
    except socket.gaierror, OSError:
        return []


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
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return
    embedded = _embedded_ipv4(ip)
    candidates = [ip] if embedded is None else [ip, embedded]
    ranges: list[tuple[list[ipaddress.IPv4Network | ipaddress.IPv6Network], str]] = []
    if block_cloud_metadata:
        ranges.append((CLOUD_METADATA_NETWORKS, "cloud metadata / link-local"))
    if block_private_networks:
        ranges.append((PRIVATE_NETWORKS, "private network"))
    for networks, label in ranges:
        matched = next((net for net in networks for c in candidates if c in net), None)
        if matched is not None:
            logger.warning(
                "SSRF protection: blocked IP",
                extra={"resolved_ip": ip_str, "range": str(matched)},
            )
            raise SsrfBlockedError(
                f"{ip_str} is in the blocked {label} range ({matched})."
            )


def assert_target_allowed(
    target: str,
    *,
    block_cloud_metadata: bool = True,
    block_private_networks: bool = False,
) -> None:
    """Resolve ``target`` and raise :class:`SsrfBlockedError` if any resolved IP is blocked.

    Call this immediately before connecting (fetch time) so it also defends against
    DNS-rebinding and redirect bypasses — not just the value configured at create time.
    """
    if not block_cloud_metadata and not block_private_networks:
        return
    for host in extract_hosts(target):
        for ip_str in resolve_host(host):
            assert_ip_allowed(
                ip_str,
                block_cloud_metadata=block_cloud_metadata,
                block_private_networks=block_private_networks,
            )
