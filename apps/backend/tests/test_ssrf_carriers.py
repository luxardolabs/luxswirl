"""Every IPv6 form that carries an IPv4 address is held to the IPv4 address's range.

A socket is dialled on an IPv4-compatible address (`::a.b.c.d`), an SIIT
IPv4-translated one (`::ffff:0:a.b.c.d`), the local-use NAT64 prefix of RFC 8215
(`64:ff9b:1::/48`, at each prefix length RFC 6052 allows) and Teredo
(`2001::/32`). On a network with a translator for one, that address reaches the
IPv4 one, so each is judged as the address it carries. Oracle's and Azure's
platform addresses serve instance metadata and are listed as such.
"""

from __future__ import annotations

import ipaddress

import pytest
from app.shared.ssrf import (
    SsrfBlockedError,
    assert_ip_allowed,
    blocked_range,
    network_cloud_metadata_overlap,
)

METADATA_CARRIERS = [
    "::169.254.169.254",  # IPv4-compatible (::/96)
    "::a9fe:a9fe",
    "::ffff:0:169.254.169.254",  # SIIT IPv4-translated (::ffff:0:0:0/96)
    "64:ff9b:1::a9fe:a9fe",  # RFC 8215, /96 operator prefix
    "64:ff9b:1:1234:5678:9abc:a9fe:a9fe",  # RFC 8215, another /96 inside the /48
    "64:ff9b:1:a9fe:a9:fe00::",  # RFC 8215, /48 operator prefix
    "64:ff9b:1:12a9:fe:a9fe::",  # RFC 8215, /56 operator prefix
    "64:ff9b:1:1234:a9:fea9:fe00:0",  # RFC 8215, /64 operator prefix
    "2001:0:102:304:0:0:5601:5601",  # Teredo, client 169.254.169.254 (obfuscated)
    "2001:0:a9fe:a9fe::1",  # Teredo, server 169.254.169.254
]


@pytest.mark.unit
@pytest.mark.parametrize("ip", METADATA_CARRIERS)
def test_an_ipv6_form_carrying_the_metadata_address_is_refused(ip: str) -> None:
    with pytest.raises(SsrfBlockedError, match="cloud metadata"):
        assert_ip_allowed(ip)


@pytest.mark.unit
@pytest.mark.parametrize(
    "ip",
    [
        "::127.0.0.1",
        "::ffff:0:10.0.0.5",
        "64:ff9b:1::c0a8:101",
        "2001:0:102:304:0:0:f5ff:fffa",  # Teredo client 10.0.0.5
    ],
)
def test_an_ipv6_form_carrying_a_private_address_follows_the_setting(ip: str) -> None:
    assert_ip_allowed(ip)
    with pytest.raises(SsrfBlockedError, match="private network"):
        assert_ip_allowed(ip, block_private_networks=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    "ip",
    [
        "::808:808",  # compat 8.8.8.8
        "::ffff:0:808:808",
        "2001:0:808:808:0:0:f7f7:f7f7",  # Teredo, server and client 8.8.8.8
        "2001:4860:4860::8888",  # an ordinary global address
    ],
)
def test_an_ipv6_form_carrying_a_public_address_is_allowed(ip: str) -> None:
    assert_ip_allowed(ip, block_private_networks=True)


@pytest.mark.unit
def test_the_local_use_nat64_prefix_is_private() -> None:
    """Its translation is the local network's own (RFC 8215), so under the block
    it is refused whatever IPv4 address it seems to carry."""
    assert_ip_allowed("64:ff9b:1::808:808")
    with pytest.raises(SsrfBlockedError, match="private network"):
        assert_ip_allowed("64:ff9b:1::808:808", block_private_networks=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    "ip",
    [
        "192.0.0.192",  # Oracle Cloud's (Classic, Cloud@Customer) metadata service
        "168.63.129.16",  # Azure's WireServer: the VM agent's platform channel
        "::ffff:168.63.129.16",
        "::ffff:0:192.0.0.192",
    ],
)
def test_the_oracle_and_azure_metadata_addresses_are_refused(ip: str) -> None:
    with pytest.raises(SsrfBlockedError, match="cloud metadata"):
        assert_ip_allowed(ip)


@pytest.mark.unit
@pytest.mark.parametrize("ip", ["192.0.0.191", "192.0.0.193", "168.63.129.15"])
def test_the_metadata_addresses_neighbours_are_allowed(ip: str) -> None:
    assert blocked_range(ip) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "network",
    [
        "::a9fe:a9fe/128",
        "::a9fe:0/112",
        "::ffff:0:a9fe:a9fe/128",
        "64:ff9b:1::a9fe:a9fe/128",
        "64:ff9b:1::a9fe:0/112",
        "64:ff9b:1:a9fe:a9:fe00::/128",
        "2001:0:102:304::5601:5601/128",
        "2001:0:102:304::5601:0/112",  # Teredo clients 169.254.0.0/16 among them
        "2001::/32",  # every Teredo client
        "64:ff9b:1::/48",
        "192.0.0.192/32",
        "168.63.129.0/24",
    ],
)
def test_a_network_carrying_a_metadata_address_overlaps_it(network: str) -> None:
    assert network_cloud_metadata_overlap(ipaddress.ip_network(network)) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "network",
    [
        "::c0a8:100/120",  # compat 192.168.1.0/24
        "::ffff:0:c0a8:100/120",
        "64:ff9b:1::c0a8:100/120",
        "2001:0:102:304::3f57:fe00/120",  # Teredo clients 192.168.1.x
        "2001:4860::/120",
    ],
)
def test_a_network_carrying_only_lan_addresses_does_not_overlap(network: str) -> None:
    assert network_cloud_metadata_overlap(ipaddress.ip_network(network)) is None


@pytest.mark.unit
def test_a_refusal_never_quotes_an_addresss_scope() -> None:
    with pytest.raises(SsrfBlockedError) as refused:
        assert_ip_allowed("fe80::1%S3CRETPW")
    assert "S3CRETPW" not in str(refused.value)
    assert "fe80::1" in str(refused.value)
