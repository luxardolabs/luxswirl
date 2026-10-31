"""A discovery result's gateway the server cannot read is logged, and the rest is kept.

The agent writes `default_gateway`; a value that is no IP address cannot be
matched to an interface. The enrichment carries on without it and says so at
WARNING, a level production keeps.
"""

from __future__ import annotations

import logging

import pytest
from app.services.core.network_discover_core_service import NetworkDiscoverCoreService


def test_unreadable_default_gateway_is_logged_and_interfaces_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    interfaces = [{"name": "eth0", "cidr": "192.168.1.10/24"}]
    with caplog.at_level(logging.DEBUG):
        enriched = NetworkDiscoverCoreService.enrich_result(
            {"default_gateway": "not-an-ip", "interfaces": interfaces}
        )

    assert enriched["interfaces"] == [{"name": "eth0", "cidr": "192.168.1.10/24"}]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "Could not match the default gateway to an interface; "
        "interfaces are left without a gateway"
    ]
    assert warnings[0].exc_info is not None


def test_readable_default_gateway_is_set_on_its_interface() -> None:
    enriched = NetworkDiscoverCoreService.enrich_result(
        {
            "default_gateway": "192.168.1.1",
            "interfaces": [{"name": "eth0", "cidr": "192.168.1.10/24"}],
        }
    )

    assert enriched["interfaces"][0]["gateway"] == "192.168.1.1"
