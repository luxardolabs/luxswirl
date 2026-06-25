"""Request-level helpers for client identification.

Provides trusted-proxy-aware client IP extraction used by:
- Rate limiter key function (per-client buckets behind a reverse proxy)
- Audit logging (auth failures, agent registrations, etc.)

The trust model: X-Forwarded-For is only honored when the direct TCP peer
is in `settings.security.trusted_proxy_networks`. This prevents an attacker
who can hit the FastAPI process directly from spoofing client IPs via the
X-Forwarded-For header.
"""

from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from ipaddress import IPv4Network, IPv6Network

    from fastapi import Request


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


def _ip_in_trusted_networks(ip_str: str) -> bool:
    try:
        ip = ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in n for n in _parsed_trusted_networks())


def client_ip_from_request(request: Request) -> str:
    """Return the real client IP, honoring X-Forwarded-For only from trusted proxies.

    Logic:
    1. Read direct TCP peer (request.client.host).
    2. If the peer is NOT in trusted_proxy_networks, return it directly —
       any X-Forwarded-For header is ignored (could be attacker-supplied).
    3. If the peer IS trusted, walk the X-Forwarded-For chain right-to-left
       (closest proxy first) and return the first hop that is NOT in
       trusted_proxy_networks — that's the original client.
    4. If the entire chain is trusted (or empty), fall back to the direct peer.
    """
    direct = request.client.host if request.client else "unknown"
    if direct == "unknown":
        return direct

    if not _ip_in_trusted_networks(direct):
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
        if not _ip_in_trusted_networks(str(ip)):
            return str(ip)

    # Whole chain is trusted (e.g., internal traffic) — return direct peer.
    return direct
