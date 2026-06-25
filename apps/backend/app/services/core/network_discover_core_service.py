"""
Network discovery job enrichment service.

Handles server-side business logic for network_discover jobs:
- Inferring networks from discovered gateways
- Generating scan suggestions
- Adding helpful warnings and recommendations
"""

import ipaddress
from typing import Any

from shared.logger import get_logger

logger = get_logger("luxswirl.services.jobs.network_discover")


class NetworkDiscoverCoreService:
    """Server-side service for network_discover job results."""

    @staticmethod
    def enrich_result(result: dict[str, Any]) -> dict[str, Any]:
        """
        Enrich network_discover job results with intelligent inference.

        Takes raw data from agent (gateways, interfaces, ARP neighbors) and adds:
        - inferred_network: Best guess at the network to scan
        - suggested_scan: Recommended scan target
        - warnings: Helpful messages about containerization, recommendations

        Args:
            result: Raw result from network_discover job

        Returns:
            Enriched result dictionary with suggestions and warnings
        """
        enriched = result.copy()

        # Get gateway information from raw agent data
        real_gateway = result.get("real_gateway")
        default_gateway = result.get("default_gateway")
        is_containerized = result.get("is_containerized", False)
        interfaces = result.get("interfaces", [])

        # Populate gateway field on interfaces where gateway is in the same network
        if default_gateway and interfaces:
            try:
                gateway_ip = ipaddress.ip_address(default_gateway)
                for iface in interfaces:
                    cidr = iface.get("cidr")
                    if cidr:
                        try:
                            network = ipaddress.ip_network(cidr, strict=False)
                            if gateway_ip in network:
                                iface["gateway"] = default_gateway
                                logger.debug(
                                    "Added gateway to interface",
                                    extra={
                                        "default_gateway": default_gateway,
                                        "interface_name": iface.get("name"),
                                        "cidr": cidr,
                                    },
                                )
                        except Exception:
                            logger.debug(
                                "Error checking gateway for interface",
                                extra={"interface_name": iface.get("name")},
                                exc_info=True,
                            )
            except Exception:
                logger.debug(
                    "Error processing default gateway",
                    extra={"default_gateway": default_gateway},
                    exc_info=True,
                )

        # Strategy 1: Infer network from real gateway (beyond Docker)
        if real_gateway:
            try:
                gateway_ip = ipaddress.ip_address(real_gateway)
                parts = str(gateway_ip).split(".")

                # Common pattern: .1 is gateway, assume /24 network
                # Conservative approach - suggest /24 to avoid scanning huge ranges
                if parts[3] == "1":
                    inferred_network = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
                else:
                    # Gateway not .1, still assume /24 for safety
                    inferred_network = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"

                enriched["inferred_network"] = inferred_network
                enriched["suggested_scan"] = inferred_network

                logger.info(
                    "Inferred network from gateway",
                    extra={
                        "inferred_network": inferred_network,
                        "real_gateway": real_gateway,
                    },
                )

                # Add helpful warning about containerization
                enriched["warning"] = (
                    f"Agent is running in container mode. "
                    f"Discovered real gateway {real_gateway} via TTL tracing. "
                    f"Suggested network: {inferred_network}. "
                    f"For full interface visibility, consider using 'network_mode: host'."
                )

            except Exception:
                logger.debug(
                    "Could not infer network from gateway",
                    extra={"real_gateway": real_gateway},
                    exc_info=True,
                )

        # Strategy 2: Use interface suggested scans (if running in host mode)
        elif interfaces:
            # Use the first interface's suggested scan (sorted by priority)
            first_iface = interfaces[0]
            if "suggested_scan" in first_iface:
                enriched["suggested_scan"] = first_iface["suggested_scan"]
                enriched["inferred_network"] = first_iface["suggested_scan"]
                logger.info(
                    "Using interface suggested scan",
                    extra={"suggested_scan": first_iface["suggested_scan"]},
                )

        # Strategy 3: No useful data - suggest host mode
        elif is_containerized and not real_gateway:
            enriched["warning"] = (
                "Agent is running in container mode without real gateway discovery. "
                "Consider using 'network_mode: host' for better network discovery."
            )

        return enriched
