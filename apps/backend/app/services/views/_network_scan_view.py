"""Display shaping for a network-scan job's detail panel.

The panel used to derive all of this in Jinja — sorting hosts, deriving the
check names a scan would create to decide whether a host is already monitored
(a 25-line block repeated twice), splitting chunks by whether they found
anything, and the response-rate percentages.
"""

from typing import Any

from app.core.scan_check_names import expected_check_names


def _pct(part: int | None, whole: int | None) -> float | None:
    """`part` as a percentage of `whole`, or None when there is no whole."""
    if not whole:
        return None
    return round((part or 0) / whole * 100, 1)


def build_scan_view(
    result: dict[str, Any] | None, existing_checks: set[str]
) -> dict[str, Any]:
    """Shape an enriched network-scan result for the detail panel.

    Args:
        result: The enriched scan result, or None for a job with no result yet.
        existing_checks: Display names of the agent's current checks.

    Returns:
        hosts sorted by IP, each carrying `all_checks_exist`; the new/monitored
        counts the filter pills show; the chunk split; and the response rate.
    """
    if not result:
        return {
            "scan_hosts": [],
            "scan_new_count": 0,
            "scan_monitored_count": 0,
            "scan_response_pct": None,
            "scan_chunks_with_findings": [],
            "scan_chunks_without_findings": [],
        }

    hosts = sorted(result.get("discovered_hosts") or [], key=lambda h: h.get("ip", ""))
    for host in hosts:
        host["all_checks_exist"] = all(
            name in existing_checks for name in expected_check_names(host)
        )

    monitored = sum(1 for host in hosts if host["all_checks_exist"])

    chunks = result.get("chunk_metrics") or []
    for chunk in chunks:
        chunk["found_pct"] = _pct(chunk.get("hosts_found"), chunk.get("hosts_scanned"))

    return {
        "scan_hosts": hosts,
        "scan_new_count": len(hosts) - monitored,
        "scan_monitored_count": monitored,
        "scan_response_pct": _pct(
            result.get("hosts_responding"), result.get("hosts_scanned")
        ),
        "scan_chunks_with_findings": [
            c for c in chunks if (c.get("hosts_found") or 0) > 0
        ],
        "scan_chunks_without_findings": [
            c for c in chunks if not (c.get("hosts_found") or 0)
        ],
    }
