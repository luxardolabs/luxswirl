"""The scan panel's derived values must match what the template used to compute.

The template decided "is this host already fully monitored?" by rebuilding the
check names a scan would create — a 25-line block, written twice, with the port
lists inline. Those lists are shared with the creation path now, so a port added
for creation is also a port the panel counts.
"""

from __future__ import annotations

from app.core.scan_panel import build_scan_view


def _host(ip, hostname=None, ports=None):
    return {"ip": ip, "hostname": hostname, "open_ports": ports or []}


def test_no_result_returns_empty_shape():
    view = build_scan_view(None, set())
    assert view["scan_hosts"] == []
    assert view["scan_new_count"] == 0
    assert view["scan_monitored_count"] == 0
    assert view["scan_response_pct"] is None


def test_hosts_sort_by_ip():
    result = {"discovered_hosts": [_host("10.0.0.9"), _host("10.0.0.1")]}
    ips = [h["ip"] for h in build_scan_view(result, set())["scan_hosts"]]
    assert ips == ["10.0.0.1", "10.0.0.9"]


def test_a_host_is_monitored_only_when_every_check_exists():
    """Ping plus one per recognised port — miss one and the host is 'new'."""
    host = _host("10.0.0.5", ports=[22, 443])
    result = {"discovered_hosts": [host]}

    partial = {"ping_10_0_0_5", "ssh_10_0_0_5_22"}
    view = build_scan_view(result, partial)
    assert view["scan_hosts"][0]["all_checks_exist"] is False
    assert (view["scan_new_count"], view["scan_monitored_count"]) == (1, 0)

    complete = partial | {"http_10_0_0_5_443"}
    view = build_scan_view(
        {"discovered_hosts": [_host("10.0.0.5", ports=[22, 443])]}, complete
    )
    assert view["scan_hosts"][0]["all_checks_exist"] is True
    assert (view["scan_new_count"], view["scan_monitored_count"]) == (0, 1)


def test_hostname_wins_over_ip_in_the_check_name():
    host = _host("10.0.0.5", hostname="db01.example.com", ports=[5432])
    view = build_scan_view(
        {"discovered_hosts": [host]},
        {"ping_db01_example_com", "db_db01_example_com_5432"},
    )
    assert view["scan_hosts"][0]["all_checks_exist"] is True


def test_unrecognised_ports_do_not_demand_a_check():
    host = _host("10.0.0.5", ports=[9999])
    view = build_scan_view({"discovered_hosts": [host]}, {"ping_10_0_0_5"})
    assert view["scan_hosts"][0]["all_checks_exist"] is True


def test_response_rate():
    view = build_scan_view({"hosts_responding": 3, "hosts_scanned": 8}, set())
    assert view["scan_response_pct"] == 37.5


def test_response_rate_is_none_when_nothing_was_scanned():
    assert (
        build_scan_view({"hosts_responding": 0, "hosts_scanned": 0}, set())[
            "scan_response_pct"
        ]
        is None
    )


def test_chunks_split_on_whether_they_found_anything():
    result = {
        "chunk_metrics": [
            {"hosts_found": 2, "hosts_scanned": 10},
            {"hosts_found": 0, "hosts_scanned": 10},
        ]
    }
    view = build_scan_view(result, set())
    assert len(view["scan_chunks_with_findings"]) == 1
    assert len(view["scan_chunks_without_findings"]) == 1
    assert view["scan_chunks_with_findings"][0]["found_pct"] == 20.0
