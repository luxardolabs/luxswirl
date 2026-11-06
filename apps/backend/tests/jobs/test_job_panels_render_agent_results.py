"""The job panels render what the agent really sends.

The agent leaves keys out (`chunks_scanned` unless it chunked, `real_gateway` and
`docker_gateway` unless it is behind a container, a category its ports did not ask
about), and the templates raise on an absent key, so every key they read has to
come through `panel_result`.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.scan_panel import panel_result
from app.services.core.network_discover_core_service import NetworkDiscoverCoreService
from app.services.core.network_scan_core_service import NetworkScanCoreService
from app.web.templates import templates

pytestmark = pytest.mark.unit

_PARTIALS = Path(__file__).resolve().parents[2] / "app/templates/web/partials/jobs"

# What apps/shared/jobs/network_discover.py writes when it is not behind a container.
DISCOVER = {
    "hostname": "agent-host",
    "default_gateway": "192.168.1.1",
    "interfaces": [
        {
            "name": "eth0",
            "ip": "192.168.1.100",
            "netmask": "255.255.255.0",
            "cidr": "192.168.1.0/24",
            "prefix_length": 24,
            "is_up": True,
        }
    ],
    "total_interfaces": 1,
    "is_containerized": False,
    "arp_neighbors": [],
    "duration_seconds": 0.1,
}


def _discover_html(result: dict[str, Any]) -> str:
    enriched = NetworkDiscoverCoreService.enrich_result(result)
    return templates.env.get_template(
        "partials/jobs/network_discover_detail.html"
    ).render(
        job=SimpleNamespace(result=enriched, agent_id="a", job_type="network_discover"),
        job_result=panel_result("network_discover", enriched),
        inferred_scan_url=None,
        discover_interfaces=[],
    )


def test_a_discover_result_from_a_plain_host_renders_its_hostname() -> None:
    html = _discover_html(DISCOVER)

    assert "agent-host" in html
    assert "No (Host mode)" in html


def test_a_discover_result_from_a_container_says_so() -> None:
    html = _discover_html(
        {
            **DISCOVER,
            "is_containerized": True,
            "real_gateway": "192.168.1.1",
            "docker_gateway": "172.17.0.1",
        }
    )

    assert "172.17.0.1" in html
    assert "No (Host mode)" not in html


def _scan_result(ports: list[int], *, chunks: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "subnet": "10.0.0.0/24",
        "hosts_scanned": 254,
        "hosts_responding": 1,
        "scan_duration_seconds": 1.0,
        "discovered_hosts": [
            {"ip": "10.0.0.1", "hostname": "h", "responds": True, "open_ports": ports}
        ],
        "scan_params": {"ports": ports, "max_concurrent": 10},
    }
    if chunks is not None:
        result["chunks_scanned"] = chunks
        result["chunk_metrics"] = []
    return result


def _scan_html(result: dict[str, Any]) -> str:
    enriched = NetworkScanCoreService.enrich_result(result)
    hosts = [{**h, "all_checks_exist": False} for h in enriched["discovered_hosts"]]
    defaults = SimpleNamespace(
        interval_seconds=60,
        timeout_seconds=10,
        retry_attempts=1,
        expected_status=200,
        verify_ssl=False,
    )
    return templates.env.get_template("partials/jobs/network_scan_detail.html").render(
        job=SimpleNamespace(
            result=enriched, agent_id="a", duration_seconds=1, job_type="network_scan"
        ),
        job_result=panel_result("network_scan", enriched),
        scan_response_pct=1,
        scan_hosts=hosts,
        scan_new_count=1,
        scan_monitored_count=0,
        scan_chunks_with_findings=[],
        scan_chunks_without_findings=[],
        existing_checks=set(),
        available_check_tags=[],
        agents=[],
        check_defaults=defaults,
        alert_defaults=defaults,
        scan_action_urls=SimpleNamespace(
            bulk_ping="/p",
            bulk_web="/w",
            bulk_ssh="/s",
            bulk_database="/d",
            quick_create="/q",
        ),
    )


@pytest.mark.parametrize("ports", [[], [80], [22], [3306], [80, 22, 3306]])
def test_a_scan_of_a_small_network_renders_whatever_its_ports(ports: list[int]) -> None:
    assert "10.0.0.1" in _scan_html(_scan_result(ports))


def test_a_chunked_scan_shows_its_chunk_count() -> None:
    assert ">7<" in _scan_html(_scan_result([80], chunks=7))


@pytest.mark.parametrize(
    ("job_type", "template"),
    [
        ("network_discover", "network_discover_detail.html"),
        ("network_scan", "network_scan_detail.html"),
    ],
)
def test_every_key_a_panel_reads_comes_through_panel_result(
    job_type: str, template: str
) -> None:
    """A key added to a template without one here would 500 on some real result."""
    source = (_PARTIALS / template).read_text()
    shaped = panel_result(job_type, {})

    for path in re.findall(r"\bresult\.([a-z_]+(?:\.[a-z_]+)?)", source):
        value: Any = shaped
        for part in path.split("."):
            assert part in value, f"{template} reads result.{path}"
            value = value[part]


def test_panel_result_keeps_what_the_agent_sent() -> None:
    shaped = panel_result(
        "network_scan", {"chunks_scanned": 3, "categorized_hosts": {"web": ["h"]}}
    )

    assert shaped["chunks_scanned"] == 3
    assert shaped["categorized_hosts"] == {"web": ["h"], "ssh": [], "database": []}
