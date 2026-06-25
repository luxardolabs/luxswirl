"""Tests for core.request_helpers — trusted-proxy-aware client IP extraction."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.request_helpers import client_ip_from_request


def _make_request(direct_ip: str | None, xff: str = "") -> SimpleNamespace:
    """Construct a minimal Request stand-in for client_ip_from_request."""
    client = SimpleNamespace(host=direct_ip) if direct_ip is not None else None
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=client, headers=headers)


@pytest.fixture(autouse=True)
def _reset_helper_caches():
    """Clear lru_cache between tests so config changes take effect."""
    from app.core.request_helpers import (
        _parsed_trusted_networks,
        _trusted_networks_cache_key,
    )

    _trusted_networks_cache_key.cache_clear()
    _parsed_trusted_networks.cache_clear()
    yield
    _trusted_networks_cache_key.cache_clear()
    _parsed_trusted_networks.cache_clear()


@pytest.fixture
def default_trusted_networks():
    """Default config: RFC 1918 + loopback as trusted."""
    nets = ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    with patch("app.core.request_helpers.settings") as mock_settings:
        mock_settings.security.trusted_proxy_networks = nets
        yield


def test_no_client_returns_unknown(default_trusted_networks):
    req = _make_request(direct_ip=None)
    assert client_ip_from_request(req) == "unknown"


def test_direct_untrusted_client_no_xff(default_trusted_networks):
    """Public IP, no proxy in front — return direct peer."""
    req = _make_request(direct_ip="93.184.216.34")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_direct_untrusted_client_ignores_spoofed_xff(default_trusted_networks):
    """Attacker hits FastAPI directly with a spoofed XFF — must be ignored."""
    req = _make_request(direct_ip="93.184.216.34", xff="1.1.1.1, 2.2.2.2")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_trusted_proxy_single_xff_hop(default_trusted_networks):
    """Behind one trusted proxy, real client is the only XFF entry."""
    req = _make_request(direct_ip="172.18.0.5", xff="93.184.216.34")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_trusted_proxy_chain_walks_to_first_untrusted(default_trusted_networks):
    """Chain of trusted proxies — first untrusted hop (right-to-left) wins."""
    # nginx (172.18.0.5) → traefik (10.0.0.5) → real client (93.184.216.34)
    req = _make_request(direct_ip="172.18.0.5", xff="93.184.216.34, 10.0.0.5")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_trusted_proxy_attacker_spoofs_first_hop(default_trusted_networks):
    """Even from a trusted proxy, the rightmost untrusted hop is the source.

    Attacker sends `X-Forwarded-For: 1.2.3.4` to the trusted proxy. The proxy
    appends the real client. We walk right-to-left and pick the rightmost
    untrusted hop — which is the proxy-appended real client, not the spoofed
    leftmost value.
    """
    # Spoofed-leftmost + real-client-rightmost (appended by trusted proxy)
    req = _make_request(direct_ip="172.18.0.5", xff="1.2.3.4, 93.184.216.34")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_trusted_proxy_empty_xff_returns_direct(default_trusted_networks):
    """Trusted proxy but no XFF header (internal request) — return proxy IP."""
    req = _make_request(direct_ip="172.18.0.5")
    assert client_ip_from_request(req) == "172.18.0.5"


def test_trusted_proxy_all_hops_trusted_returns_direct(default_trusted_networks):
    """All XFF hops are internal — return direct peer."""
    req = _make_request(direct_ip="172.18.0.5", xff="10.0.0.7, 192.168.1.4")
    assert client_ip_from_request(req) == "172.18.0.5"


def test_malformed_xff_hop_skipped(default_trusted_networks):
    """Garbage hop in XFF is skipped, not crashed."""
    req = _make_request(direct_ip="172.18.0.5", xff="not-an-ip, 93.184.216.34")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_xff_whitespace_handling(default_trusted_networks):
    """Leading/trailing whitespace around hops is tolerated."""
    req = _make_request(direct_ip="172.18.0.5", xff="  93.184.216.34  ,  10.0.0.5  ")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_loopback_treated_as_trusted(default_trusted_networks):
    """127.0.0.1 is in trusted defaults (test harness, dev)."""
    req = _make_request(direct_ip="127.0.0.1", xff="93.184.216.34")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_ipv6_trusted_proxy(default_trusted_networks):
    """IPv6 loopback peer trusts XFF — works without crashing."""
    req = _make_request(direct_ip="::1", xff="93.184.216.34")
    assert client_ip_from_request(req) == "93.184.216.34"


def test_empty_trusted_proxy_list_disables_xff_honoring():
    """If operator clears the trusted list, XFF is never honored."""
    with patch("app.core.request_helpers.settings") as mock_settings:
        mock_settings.security.trusted_proxy_networks = []
        from app.core.request_helpers import (
            _parsed_trusted_networks,
            _trusted_networks_cache_key,
        )

        _trusted_networks_cache_key.cache_clear()
        _parsed_trusted_networks.cache_clear()

        req = _make_request(direct_ip="127.0.0.1", xff="93.184.216.34")
        # Even loopback isn't trusted now — direct peer wins.
        assert client_ip_from_request(req) == "127.0.0.1"


def test_misconfigured_cidr_does_not_crash():
    """A typo'd CIDR is silently dropped, valid ones still apply."""
    with patch("app.core.request_helpers.settings") as mock_settings:
        mock_settings.security.trusted_proxy_networks = [
            "not-a-cidr",
            "127.0.0.0/8",
        ]
        from app.core.request_helpers import (
            _parsed_trusted_networks,
            _trusted_networks_cache_key,
        )

        _trusted_networks_cache_key.cache_clear()
        _parsed_trusted_networks.cache_clear()

        req = _make_request(direct_ip="127.0.0.1", xff="93.184.216.34")
        # Loopback still trusted via the valid CIDR, so XFF honored.
        assert client_ip_from_request(req) == "93.184.216.34"
