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
from urllib.parse import urlparse

from shared.logger import get_logger

logger = get_logger("luxswirl.security.ssrf")

# Cloud metadata + link-local — dangerous in any cloud/container environment.
CLOUD_METADATA_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure IMDS + link-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]

# RFC 1918 private networks — where most self-hosted targets live.
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
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
    ranges: list[tuple[list[ipaddress._BaseNetwork], str]] = []
    if block_cloud_metadata:
        ranges.append((CLOUD_METADATA_NETWORKS, "cloud metadata / link-local"))
    if block_private_networks:
        ranges.append((PRIVATE_NETWORKS, "private network"))
    for networks, label in ranges:
        matched = next((net for net in networks if ip in net), None)
        if matched is not None:
            logger.warning(
                "SSRF protection: blocked IP",
                extra={"resolved_ip": ip_str, "range": str(matched)},
            )
            raise SsrfBlockedError(f"{ip_str} is in the blocked {label} range ({matched}).")


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
    host = extract_host(target)
    if not host:
        return
    for ip_str in resolve_host(host):
        assert_ip_allowed(
            ip_str,
            block_cloud_metadata=block_cloud_metadata,
            block_private_networks=block_private_networks,
        )
