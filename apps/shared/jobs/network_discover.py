"""
Network discovery job - discover agent's network topology.

Returns information about the agent's network interfaces, making it easy
for users to choose which networks to scan.
"""

import ipaddress
import os
import socket
import struct
import time
from typing import Any

from shared.subprocess_safe import get_subprocess_config, run_subprocess_safely
from pydantic import BaseModel

from shared.jobs.base import BaseJob


class NetworkDiscoverParams(BaseModel):
    """
    Parameter schema for network_discover job.

    This job requires no parameters - it automatically discovers the agent's network topology.
    """


class NetworkDiscoverJob(BaseJob):
    """
    Network topology discovery job.

    Analyzes the agent's network interfaces and returns information
    about available networks for scanning.

    Job-level configuration:
    - default_timeout_seconds: Maximum time for discovery operation
    - params_schema: Pydantic model (empty - no params required)
    """

    job_type = "network_discover"
    params_schema = NetworkDiscoverParams

    # Job-level timeout: Network discovery is typically fast (< 5 seconds)
    default_timeout_seconds = 30

    # Execution requirements
    requires_agent = True  # Cannot run on server (needs network interface access)

    # Display metadata
    display_name = "Network Discovery"
    display_description = "Discover the agent's network topology and suggest scan targets"

    async def execute(self) -> dict[str, Any]:
        """
        Discover network topology.

        Returns:
            Dictionary with network interface information:
            {
                "interfaces": [
                    {
                        "name": "eth0",
                        "ip": "192.168.1.100",
                        "netmask": "255.255.255.0",
                        "cidr": "192.168.1.0/24",
                        "gateway": "192.168.1.1",
                        "is_up": true,
                        "suggested_scan": "192.168.1.0/24"
                    }
                ],
                "hostname": "agent-host",
                "default_gateway": "192.168.1.1",
                "duration_seconds": 1.23
            }
        """
        start_time = time.time()
        self.logger.info("Discovering network topology")

        interfaces = []
        hostname = socket.gethostname()
        default_gateway = None
        is_containerized = self._is_containerized()
        arp_neighbors = []

        # Try multiple methods to get default gateway
        real_gateway = None
        docker_gateway = None

        try:
            default_gateway = await self._get_default_gateway()
        except Exception:
            self.logger.debug("Could not get gateway via 'ip route'", exc_info=True)

        if not default_gateway:
            try:
                default_gateway = await self._get_default_gateway_from_proc()
            except Exception:
                self.logger.debug("Could not get gateway from /proc/net/route", exc_info=True)

        # If we found a gateway, check if it's a Docker bridge
        if default_gateway and self._is_docker_bridge_network(f"{default_gateway}/32"):
            docker_gateway = default_gateway
            self.logger.info(
                "Found Docker bridge gateway",
                extra={"docker_gateway": docker_gateway},
            )

            # Try to discover the real gateway beyond Docker using TTL tracing
            try:
                real_gateway = await self._discover_real_gateway_via_ttl()
                if real_gateway:
                    default_gateway = real_gateway  # Use real gateway as primary
            except Exception:
                self.logger.debug("Could not discover real gateway via TTL", exc_info=True)

        # Get ARP table for active network detection
        try:
            arp_neighbors = await self._get_arp_table()
            if arp_neighbors:
                self.logger.info(
                    "Found active neighbors in ARP table",
                    extra={"neighbor_count": len(arp_neighbors)},
                )
        except Exception:
            self.logger.debug("Could not read ARP table", exc_info=True)

        # Get network interfaces
        try:
            interfaces = await self._get_interfaces_linux()
        except Exception:
            self.logger.warning(
                "Could not get interfaces via 'ip' command",
                exc_info=True,
            )
            # Fallback to socket-based detection
            interfaces = await self._get_interfaces_fallback()

        # Filter and enrich interface data
        filtered_interfaces = []
        docker_interfaces = []

        for iface in interfaces:
            # Skip loopback
            if iface.get("ip", "").startswith("127."):
                continue

            # Skip link-local
            if iface.get("ip", "").startswith("169.254."):
                continue

            # Check if this is a Docker bridge network
            cidr = iface.get("cidr")
            if cidr and self._is_docker_bridge_network(cidr):
                iface["is_docker_bridge"] = True
                docker_interfaces.append(iface)
                continue  # Skip Docker bridges by default

            # Add suggested scan range
            if cidr:
                try:
                    network = ipaddress.ip_network(cidr, strict=False)
                    # Suggest smaller ranges for large networks
                    if network.num_addresses > 256:
                        # For /16 or larger, suggest /24 based on the interface IP
                        ip_parts = iface["ip"].split(".")
                        suggested = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0/24"
                    else:
                        suggested = str(network)

                    iface["suggested_scan"] = suggested
                    iface["network_size"] = network.num_addresses
                    iface["is_docker_bridge"] = False

                    # Check if this network has the gateway (probably the real network)
                    if default_gateway:
                        try:
                            if ipaddress.ip_address(default_gateway) in network:
                                iface["has_gateway"] = True
                                iface["priority"] = 100  # Highest priority
                        except Exception:
                            pass

                except ValueError:
                    pass

            filtered_interfaces.append(iface)

        # Sort by priority (networks with gateways first)
        filtered_interfaces.sort(key=lambda x: x.get("priority", 0), reverse=True)

        self.logger.info(
            "Discovered scannable networks",
            extra={
                "scannable_count": len(filtered_interfaces),
                "docker_bridges_filtered": len(docker_interfaces),
            },
        )

        # Calculate execution duration
        duration = time.time() - start_time

        result = {
            "hostname": hostname,
            "default_gateway": default_gateway,
            "interfaces": filtered_interfaces,
            "total_interfaces": len(filtered_interfaces),
            "is_containerized": is_containerized,
            "arp_neighbors": arp_neighbors,
            "duration_seconds": duration,
        }

        # If we discovered a real gateway via TTL, include it in raw data
        if real_gateway:
            result["real_gateway"] = real_gateway
            result["docker_gateway"] = docker_gateway

        # Return raw data - let the server do the intelligence/inference
        return result

    async def _get_default_gateway(self) -> str | None:
        """Get the default gateway IP address."""
        subprocess_config = get_subprocess_config({})
        cmd_timeout = 5  # Default timeout for subprocess commands

        try:
            # Try 'ip route' on Linux
            returncode, stdout, _ = await run_subprocess_safely(
                "ip",
                "route",
                "show",
                "default",
                timeout=float(cmd_timeout),
                capture_output=True,
                grace_seconds=subprocess_config["grace_seconds"],
                kill_timeout=subprocess_config["kill_timeout"],
            )

            if returncode == 0:
                output = (stdout or b"").decode().strip()
                # Parse: "default via 192.168.1.1 dev eth0"
                parts = output.split()
                if "via" in parts:
                    idx = parts.index("via")
                    if idx + 1 < len(parts):
                        return parts[idx + 1]
        except Exception:
            pass

        try:
            # Try 'route' on Mac/BSD
            returncode, stdout, _ = await run_subprocess_safely(
                "route",
                "-n",
                "get",
                "default",
                timeout=float(cmd_timeout),
                capture_output=True,
                grace_seconds=subprocess_config["grace_seconds"],
                kill_timeout=subprocess_config["kill_timeout"],
            )

            if returncode == 0:
                output = (stdout or b"").decode()
                for line in output.split("\n"):
                    if "gateway:" in line.lower():
                        return line.split(":")[-1].strip()
        except Exception:
            pass

        return None

    async def _get_default_gateway_from_proc(self) -> str | None:
        """Get default gateway by parsing /proc/net/route."""
        try:
            with open("/proc/net/route") as f:
                for line in f:
                    fields = line.strip().split()
                    if len(fields) < 3:
                        continue
                    # Check if destination is 00000000 (default route)
                    if fields[1] == "00000000":
                        # Gateway is in hex, little-endian format
                        gateway_hex = fields[2]
                        # Convert hex to IP (reverse byte order)
                        gateway_int = int(gateway_hex, 16)
                        gateway_ip = socket.inet_ntoa(struct.pack("<L", gateway_int))
                        return gateway_ip
        except Exception:
            self.logger.debug("Could not parse /proc/net/route", exc_info=True)
        return None

    async def _discover_real_gateway_via_ttl(self) -> str | None:
        """
        Discover the real gateway beyond Docker by using TTL-based tracing.

        Sends ping with TTL=2 to external IP (8.8.8.8) and parses the
        "Time to live exceeded" response to find the real gateway.
        """
        subprocess_config = get_subprocess_config({})
        cmd_timeout = 5  # Default timeout for subprocess commands

        try:
            # Ping Google DNS with TTL=2 to get second hop (real gateway)
            returncode, stdout, _ = await run_subprocess_safely(
                "ping",
                "-c",
                "1",
                "-t",
                "2",
                "8.8.8.8",
                timeout=float(cmd_timeout),
                capture_output=True,
                grace_seconds=subprocess_config["grace_seconds"],
                kill_timeout=subprocess_config["kill_timeout"],
            )

            output = (stdout or b"").decode()

            # Parse output for "From X.X.X.X icmp_seq=1 Time to live exceeded"
            for line in output.split("\n"):
                if "Time to live exceeded" in line or "TTL exceeded" in line:
                    # Extract IP from "From 10.10.0.1 icmp_seq=1 ..."
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == "From":
                        gateway_ip = parts[1]
                        # Validate it's an IP address
                        try:
                            ipaddress.ip_address(gateway_ip)
                            self.logger.info(
                                "Discovered real gateway via TTL trace",
                                extra={"gateway_ip": gateway_ip},
                            )
                            return gateway_ip
                        except ValueError:
                            continue
        except Exception:
            self.logger.debug("TTL-based gateway discovery failed", exc_info=True)

        return None

    async def _get_arp_table(self) -> list[str]:
        """Get list of IPs from ARP table (active network neighbors)."""
        ips = []
        try:
            with open("/proc/net/arp") as f:
                for line in f:
                    if line.startswith("IP"):  # Skip header
                        continue
                    fields = line.strip().split()
                    if len(fields) > 0:
                        ip = fields[0]
                        # Basic IP validation
                        if "." in ip and not ip.startswith("0."):
                            ips.append(ip)
        except Exception:
            self.logger.debug("Could not read ARP table", exc_info=True)
        return ips

    def _is_docker_bridge_network(self, cidr: str) -> bool:
        """Check if network is a Docker bridge network."""
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            # Docker uses 172.16.0.0/12 for bridge networks
            docker_range = ipaddress.IPv4Network("172.16.0.0/12")
            # Only check IPv4 networks against Docker range
            if isinstance(network, ipaddress.IPv4Network):
                return network.subnet_of(docker_range) or network == docker_range
            return False
        except Exception:
            return False

    def _is_containerized(self) -> bool:
        """Detect if running in a container."""
        # Check for .dockerenv file
        if os.path.exists("/.dockerenv"):
            return True
        # Check cgroup for docker/containerd
        try:
            with open("/proc/1/cgroup") as f:
                content = f.read()
                if "docker" in content or "containerd" in content:
                    return True
        except Exception:
            pass
        return False

    async def _get_interfaces_linux(self) -> list[dict[str, Any]]:
        """Get network interfaces using 'ip' command (Linux)."""
        interfaces = []
        subprocess_config = get_subprocess_config({})
        cmd_timeout = 5  # Default timeout for subprocess commands

        # Get interface list
        returncode, stdout, _ = await run_subprocess_safely(
            "ip",
            "-o",
            "-4",
            "addr",
            "show",
            timeout=float(cmd_timeout),
            capture_output=True,
            grace_seconds=subprocess_config["grace_seconds"],
            kill_timeout=subprocess_config["kill_timeout"],
        )

        if returncode != 0:
            raise RuntimeError("ip command failed")

        # Parse output
        # Format: "2: eth0    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0"
        for line in (stdout or b"").decode().split("\n"):
            if not line.strip():
                continue

            parts = line.split()
            if len(parts) < 4:
                continue

            iface_name = parts[1]
            if "inet" not in parts:
                continue

            inet_idx = parts.index("inet")
            if inet_idx + 1 >= len(parts):
                continue

            ip_cidr = parts[inet_idx + 1]  # "192.168.1.100/24"

            try:
                ip_addr, prefix_len = ip_cidr.split("/")
                network = ipaddress.ip_network(f"{ip_addr}/{prefix_len}", strict=False)

                interfaces.append(
                    {
                        "name": iface_name,
                        "ip": ip_addr,
                        "netmask": str(network.netmask),
                        "cidr": str(network),
                        "prefix_length": int(prefix_len),
                        "is_up": True,
                    }
                )
            except Exception:
                self.logger.warning(
                    "Could not parse interface",
                    extra={"interface_name": iface_name},
                    exc_info=True,
                )
                continue

        return interfaces

    async def _get_interfaces_fallback(self) -> list[dict[str, Any]]:
        """Fallback method using socket module."""
        interfaces = []

        # Get hostname and resolve it
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)

            # Assume /24 network
            network = ipaddress.ip_network(f"{local_ip}/24", strict=False)

            interfaces.append(
                {
                    "name": "default",
                    "ip": local_ip,
                    "netmask": "255.255.255.0",
                    "cidr": str(network),
                    "prefix_length": 24,
                    "is_up": True,
                }
            )
        except Exception:
            self.logger.warning("Fallback interface detection failed", exc_info=True)

        return interfaces
