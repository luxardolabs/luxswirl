"""Request-level helpers for client identification.

Provides trusted-proxy-aware client IP extraction used by:
- Rate limiter key function (per-client buckets behind a reverse proxy)
- Audit logging (auth failures, agent registrations, etc.)

The trust model: X-Forwarded-For is only honored when the direct TCP peer
is a trusted proxy: within `settings.security.trusted_proxy_networks`, or one
of the addresses a name in `settings.security.trusted_proxy_hosts` resolves to.
This prevents a client that can reach the FastAPI process directly, from the
internet or from a private network beside it, from choosing its own
rate-limit bucket via the X-Forwarded-For header.

The names are resolved off the request path, by `refresh_trusted_proxy_hosts`,
which the server runs at startup and every PROXY_HOSTS_REFRESH_SECONDS after
(`app/background/trusted_proxy_refresh.py`), so a request never waits on DNS.
Until a name resolves, its proxy is not trusted: its clients share the proxy's
bucket, which is safe, not open.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING

from app.core.config import settings
from app.shared.logger import get_logger

if TYPE_CHECKING:
    from ipaddress import IPv4Network, IPv6Network

    from fastapi import Request

logger = get_logger("luxswirl.core.request_helpers")

# How often the proxy names are looked up again: a restarted proxy container
# can come back on another address.
PROXY_HOSTS_REFRESH_SECONDS = 30.0

_proxy_host_ips: frozenset[str] = frozenset()


@lru_cache(maxsize=1)
def _trusted_networks_cache_key() -> tuple[str, ...]:
    """Cache key based on the current configured CIDRs (so updates invalidate)."""
    return tuple(settings.security.trusted_proxy_networks)


@lru_cache(maxsize=1)
def _parsed_trusted_networks() -> list[IPv4Network | IPv6Network]:
    """Parsed CIDR list, cached. Invalidates when the config tuple changes."""
    nets: list[IPv4Network | IPv6Network] = []
    for cidr in _trusted_networks_cache_key():
        try:
            nets.append(ip_network(cidr, strict=False))
        except ValueError:
            # Misconfigured CIDR — skip silently rather than crash startup.
            # Operators see the typo in /settings if surfaced.
            continue
    return nets


def reset_trusted_networks_cache() -> None:
    """Clear the trusted-proxy caches so a config change takes effect.

    Public entry point for cache invalidation — used by tests (which mutate
    the configured CIDRs and hosts between cases) and available to runtime code
    that reconfigures the trusted proxies without a restart.
    """
    global _proxy_host_ips
    _trusted_networks_cache_key.cache_clear()
    _parsed_trusted_networks.cache_clear()
    _proxy_host_ips = frozenset()


async def _resolve_host(host: str) -> set[str]:
    """Every address `host` resolves to, without blocking the event loop."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return {str(info[4][0]) for info in infos}


async def refresh_trusted_proxy_hosts() -> None:
    """Resolve `trusted_proxy_hosts` and trust exactly the addresses found.

    A name that does not resolve (no such proxy in this deployment) adds
    nothing; the others still count.
    """
    global _proxy_host_ips
    found: set[str] = set()
    for host in settings.security.trusted_proxy_hosts:
        try:
            found |= await _resolve_host(host)
        except OSError as exc:
            logger.debug(
                "Trusted proxy host did not resolve",
                extra={"host": host, "error": type(exc).__name__},
            )
    _proxy_host_ips = frozenset(found)


def _is_trusted_proxy(ip_str: str) -> bool:
    try:
        ip = ip_address(ip_str)
    except ValueError:
        return False
    if str(ip) in _proxy_host_ips:
        return True
    return any(ip in n for n in _parsed_trusted_networks())


def client_ip_from_request(request: Request) -> str:
    """Return the real client IP, honoring X-Forwarded-For only from trusted proxies.

    Logic:
    1. Read direct TCP peer (request.client.host).
    2. If the peer is NOT a trusted proxy, return it directly — any
       X-Forwarded-For header is ignored (could be attacker-supplied).
    3. If the peer IS trusted, walk the X-Forwarded-For chain right-to-left
       (closest proxy first) and return the first hop that is NOT a trusted
       proxy — that's the original client.
    4. If the entire chain is trusted (or empty), fall back to the direct peer.
    """
    direct = request.client.host if request.client else "unknown"
    if direct == "unknown":
        return direct

    if not _is_trusted_proxy(direct):
        # Not behind a trusted proxy — ignore XFF entirely.
        return direct

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return direct

    # Walk right-to-left: the rightmost hop was added by the closest proxy.
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ip = ip_address(hop)
        except ValueError:
            # Malformed hop — keep walking.
            continue
        if not _is_trusted_proxy(str(ip)):
            return str(ip)

    # Whole chain is trusted (e.g., internal traffic) — return direct peer.
    return direct
