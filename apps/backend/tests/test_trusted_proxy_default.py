"""By default X-Forwarded-For is taken only from the stack's own proxy.

The default `trusted_proxy_networks` was every RFC 1918 range, so any peer on a
private network (a LAN host reaching the server's port, a container beside it)
could send a new forged `X-Forwarded-For` on each request and get a fresh
rate-limit bucket every time: no login, registration or heartbeat limit keyed
by address ever reached it. The default now trusts loopback (a proxy on the
same host) and the addresses the shipped proxy's service name resolves to.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.core import request_helpers
from app.core.config import SecuritySettings
from app.core.request_helpers import (
    client_ip_from_request,
    refresh_trusted_proxy_hosts,
    reset_trusted_networks_cache,
)

pytestmark = pytest.mark.unit

PRIVATE_PEERS = ["10.0.0.5", "172.18.0.7", "192.168.1.20"]


def _request(peer: str, xff: str) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff}
    )


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch):
    """The shipped defaults, not whatever the test environment configured."""
    monkeypatch.setattr(
        request_helpers.settings.security,
        "trusted_proxy_networks",
        SecuritySettings.model_fields["trusted_proxy_networks"].default,
    )
    monkeypatch.setattr(
        request_helpers.settings.security,
        "trusted_proxy_hosts",
        SecuritySettings.model_fields["trusted_proxy_hosts"].default,
    )
    reset_trusted_networks_cache()
    yield
    reset_trusted_networks_cache()


def test_the_default_trusts_no_private_range() -> None:
    nets = SecuritySettings.model_fields["trusted_proxy_networks"].default
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert private not in nets


@pytest.mark.parametrize("peer", PRIVATE_PEERS)
def test_a_private_peer_cannot_rotate_its_bucket(peer: str) -> None:
    keys = {client_ip_from_request(_request(peer, f"203.0.113.{n}")) for n in range(20)}

    assert keys == {peer}


def test_a_proxy_on_the_same_host_is_still_trusted() -> None:
    assert client_ip_from_request(_request("127.0.0.1", "203.0.113.9")) == (
        "203.0.113.9"
    )


async def test_a_proxy_host_is_trusted_at_the_address_it_resolves_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`localhost` resolves from the hosts file, so no DNS is involved; with no
    network trusted, loopback is trusted only through the name."""
    monkeypatch.setattr(request_helpers.settings.security, "trusted_proxy_networks", [])
    monkeypatch.setattr(
        request_helpers.settings.security, "trusted_proxy_hosts", ["localhost"]
    )
    reset_trusted_networks_cache()
    before = client_ip_from_request(_request("127.0.0.1", "203.0.113.9"))

    await refresh_trusted_proxy_hosts()

    assert before == "127.0.0.1", "trusted before the name was resolved"
    assert client_ip_from_request(_request("127.0.0.1", "203.0.113.9")) == (
        "203.0.113.9"
    )
    # Another peer is not the proxy.
    assert client_ip_from_request(_request("172.18.0.7", "203.0.113.9")) == (
        "172.18.0.7"
    )


async def test_an_unresolvable_proxy_host_trusts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(request_helpers.settings.security, "trusted_proxy_networks", [])
    monkeypatch.setattr(
        request_helpers.settings.security,
        "trusted_proxy_hosts",
        ["no-such-proxy.invalid", "localhost"],
    )
    reset_trusted_networks_cache()

    await refresh_trusted_proxy_hosts()

    # The name that resolves still counts; the one that does not adds nothing.
    assert client_ip_from_request(_request("127.0.0.1", "203.0.113.9")) == (
        "203.0.113.9"
    )
    assert client_ip_from_request(_request("172.18.0.9", "203.0.113.9")) == (
        "172.18.0.9"
    )
