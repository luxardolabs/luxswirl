"""
Network scan job enrichment service.

Handles server-side business logic for network_scan jobs:
- Categorizing discovered hosts
- Generating summary statistics
- Identifying interesting devices
- Creating follow-up recommendations
"""

import ipaddress
import re
from typing import Any

from app.core.scan_check_names import PORT_CATEGORIES
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.jobs.network_scan")


# One DNS label: letters, digits, hyphens and (as reverse DNS on Windows
# networks returns them) underscores, 1-63 long, never starting or ending with
# a hyphen. Nothing a URL reads as userinfo, port, path or query can pass.
_LABEL = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?")
_MAX_HOSTNAME = 253

# The keys `enrich_result` derives. An agent's result that names one is never
# read: the server builds each from the hosts `scan_hosts` validated.
_DERIVED_KEYS = frozenset(
    {
        "summary",
        "host_categories",
        "recommendations",
        "hosts_by_category",
        "categorized_hosts",
        "port_aware_summary",
        "port_aware_recommendations",
    }
)


def _ip(value: object) -> str | None:
    """`value` if it is an IP address, as Python writes it; nothing otherwise."""
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _hostname(value: object) -> str | None:
    """`value` if it is a DNS hostname; nothing otherwise."""
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_HOSTNAME:
        return None
    if all(_LABEL.fullmatch(label) for label in value.split(".")):
        return value
    return None


def _port_numbers(value: object) -> list[int]:
    """The port numbers in `value`, if it is a list; nothing otherwise."""
    if not isinstance(value, list):
        return []
    return [p for p in value if isinstance(p, int) and not isinstance(p, bool)]


class NetworkScanCoreService:
    """Server-side service for network_scan job results."""

    @staticmethod
    def scan_hosts(result: dict[str, Any]) -> list[dict[str, Any]]:
        """The scan's hosts, each a mapping whose `ip` is an IP address or
        None, `hostname` a DNS hostname or None, and `open_ports` port numbers.

        The agent writes the result, and a check made from a host connects to
        its hostname, else its ip: an entry that is not a mapping is dropped,
        an `ip` or `hostname` that is not one is None (a hostname such as
        "10.1.2.3@10.9.9.9" would make a URL that connects to another host),
        and a port that is not a number is left out.
        """
        hosts = result.get("discovered_hosts")
        if not isinstance(hosts, list):
            return []
        return [
            {
                **host,
                "ip": _ip(host.get("ip")),
                "hostname": _hostname(host.get("hostname")),
                "open_ports": _port_numbers(host.get("open_ports")),
            }
            for host in hosts
            if isinstance(host, dict)
        ]

    @staticmethod
    def enrich_result(result: dict[str, Any]) -> dict[str, Any]:
        """
        Enrich network_scan job results with analysis and recommendations.

        Takes raw scan data from agent (discovered hosts, ports, etc.) and adds:
        - host_categories: Categorize hosts by open ports (web servers, SSH, etc.)
        - summary_stats: Quick overview of what was found
        - recommendations: Suggested next steps (deeper scans, check creation, etc.)

        Args:
            result: Raw result from network_scan job

        Returns:
            Enriched result with analysis and recommendations

        The agent writes the result, and the check creators read
        `categorized_hosts`: every key derived here (`_DERIVED_KEYS`) is the
        server's own, built from the validated hosts, never the agent's copy.
        """
        enriched = {k: v for k, v in result.items() if k not in _DERIVED_KEYS}

        discovered_hosts = NetworkScanCoreService.scan_hosts(result)
        enriched["discovered_hosts"] = discovered_hosts
        enriched.update(
            NetworkScanCoreService._categorize_by_scanned_ports(
                result, discovered_hosts
            )
        )

        if not discovered_hosts:
            enriched["summary"] = "No hosts discovered on network"
            enriched["hosts_by_category"] = {
                "web_servers": [],
                "ssh_servers": [],
                "database_servers": [],
                "other_hosts": [],
            }
            return enriched

        # Categorize hosts by services
        web_servers = []
        ssh_servers = []
        database_servers = []
        other_hosts = []

        for host in discovered_hosts:
            open_ports = host.get("open_ports", [])

            # Categorize by common ports
            if 80 in open_ports or 443 in open_ports or 8080 in open_ports:
                web_servers.append(host)
            elif 22 in open_ports:
                ssh_servers.append(host)
            elif any(p in open_ports for p in [3306, 5432, 27017, 6379]):
                database_servers.append(host)
            elif open_ports:
                other_hosts.append(host)

        # Add categorization
        enriched["host_categories"] = {
            "web_servers": len(web_servers),
            "ssh_servers": len(ssh_servers),
            "database_servers": len(database_servers),
            "other_services": len(other_hosts),
            "no_open_ports": len(
                [h for h in discovered_hosts if not h.get("open_ports")]
            ),
        }

        # Generate summary
        total_hosts = len(discovered_hosts)

        enriched["summary"] = (
            f"Discovered {total_hosts} host(s): "
            f"{len(web_servers)} web, "
            f"{len(ssh_servers)} SSH, "
            f"{len(database_servers)} database, "
            f"{len(other_hosts)} other services"
        )

        # Generate recommendations
        recommendations = []

        if web_servers:
            recommendations.append(
                f"Create HTTP checks for {len(web_servers)} web server(s) discovered"
            )

        if ssh_servers:
            recommendations.append(
                f"Create TCP checks for {len(ssh_servers)} SSH server(s) discovered"
            )

        if database_servers:
            recommendations.append(
                f"Review {len(database_servers)} database server(s) - ensure they're not publicly exposed"
            )

        if recommendations:
            enriched["recommendations"] = recommendations
            logger.info(
                "Generated recommendations for scan results",
                extra={"recommendation_count": len(recommendations)},
            )

        # Add detailed host list for UI display
        enriched["hosts_by_category"] = {
            "web_servers": web_servers,
            "ssh_servers": ssh_servers,
            "database_servers": database_servers,
            "other_hosts": other_hosts,
        }

        logger.info(
            "Enriched network_scan",
            extra={
                "total_hosts": total_hosts,
                "web_count": len(web_servers),
                "ssh_count": len(ssh_servers),
                "db_count": len(database_servers),
            },
        )

        return enriched

    @staticmethod
    def _categorize_by_scanned_ports(
        result: dict[str, Any], discovered_hosts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Build the `categorized_hosts` view-friendly mapping based on the ports
        actually scanned in this run.

        Unlike the broader hard-coded categorization above, this respects what
        the scan_params asked about — categories with no scanned ports are
        omitted, and a host can appear in multiple categories.

        Returns a dict to be merged into the enriched result with these keys:
        - categorized_hosts: dict[category, list[host]]
        - port_aware_summary: str (human-readable counts)
        - port_aware_recommendations: list[str]
        """
        scan_params = result.get("scan_params")
        scanned_ports = _port_numbers(
            scan_params.get("ports") if isinstance(scan_params, dict) else None
        )

        # Shared with the creation path and the scan panel; only categories with at
        # least one scanned port survive.
        port_categories: dict[str, list[int]] = {**PORT_CATEGORIES, "other": []}
        active_categories: dict[str, list[int]] = {}
        for category, ports in port_categories.items():
            if category == "other":
                active_categories[category] = []
            else:
                active_ports = [p for p in ports if p in scanned_ports]
                if active_ports:
                    active_categories[category] = active_ports

        categorized: dict[str, list[dict[str, Any]]] = {
            cat: [] for cat in active_categories
        }

        for host in discovered_hosts:
            open_ports = host.get("open_ports", [])
            if not open_ports:
                continue
            host_categories: set[str] = set()
            for port in open_ports:
                matched = False
                for category, category_ports in active_categories.items():
                    if category == "other":
                        continue
                    if port in category_ports:
                        host_categories.add(category)
                        matched = True
                        break
                if not matched:
                    host_categories.add("other")
            for category in host_categories:
                if category in categorized:
                    categorized[category].append(host)

        summary_parts = [
            f"{len(hosts)} {cat}" for cat, hosts in categorized.items() if hosts
        ]
        port_aware_summary = (
            f"Discovered {len(discovered_hosts)} host(s): {', '.join(summary_parts)}"
            if summary_parts
            else f"Discovered {len(discovered_hosts)} host(s) — none in scanned categories"
        )

        recommendations: list[str] = []
        if categorized.get("web"):
            recommendations.append(
                f"Create HTTP checks for {len(categorized['web'])} web server(s) discovered"
            )
        if categorized.get("ssh"):
            recommendations.append(
                f"Create TCP checks for {len(categorized['ssh'])} SSH server(s) discovered"
            )
        if categorized.get("database"):
            recommendations.append(
                f"Consider creating database health checks for {len(categorized['database'])} database server(s)"
            )

        return {
            "categorized_hosts": categorized,
            "port_aware_summary": port_aware_summary,
            "port_aware_recommendations": recommendations,
        }
