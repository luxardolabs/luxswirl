"""
Network scan job - discover hosts on a subnet.

Performs:
- ICMP ping sweep to find responsive hosts
- Optional port scanning on discovered hosts
- Hostname resolution via reverse DNS
"""

import asyncio
import ipaddress
import platform
import socket
import time
from typing import Any

from shared.subprocess_safe import get_subprocess_config, run_subprocess_no_output
from pydantic import BaseModel, Field, field_validator

from shared.jobs.base import BaseJob


class NetworkScanParams(BaseModel):
    """
    Parameter schema for network_scan job.

    Defines all configurable parameters with types, defaults, validation, and documentation.
    """

    subnet: str = Field(
        ...,  # Required field
        description="Network to scan in CIDR notation",
        json_schema_extra={"examples": ["192.168.1.0/24", "10.0.0.0/16"]},
    )

    timeout: int = Field(
        10,
        ge=1,
        le=30,
        description="Total timeout per host in seconds. Each host gets this much time for all operations combined (ping + DNS lookup + port scan). Default 10s works for most networks.",
    )

    ports: list[int] = Field(
        default=[22, 80, 443, 3306, 5432, 8080, 8443],
        description="TCP ports to scan on discovered hosts (empty list = ping only)",
    )

    max_concurrent: int = Field(
        100,
        ge=10,
        le=500,
        description="Maximum concurrent host scans (higher = faster but more CPU/network load)",
    )

    @field_validator("subnet")
    @classmethod
    def validate_subnet(cls, v: str) -> str:
        """Validate CIDR notation."""
        try:
            network = ipaddress.ip_network(v, strict=False)
            if network.num_addresses > 65536:
                raise ValueError(
                    f"Network too large ({network.num_addresses} hosts). Maximum is 65536 (/16)"
                )
            return v
        except ValueError as e:
            raise ValueError(f"Invalid subnet CIDR notation: {e}") from e

    @field_validator("ports")
    @classmethod
    def validate_ports(cls, v: list[int]) -> list[int]:
        """Validate port numbers."""
        if v:
            for port in v:
                if not (1 <= port <= 65535):
                    raise ValueError(f"Invalid port number: {port}. Must be between 1 and 65535")
        return v


class NetworkScanJob(BaseJob):
    """
    Network scanner job.

    Scans a subnet for responsive hosts and optionally probes common ports.

    Job-level configuration:
    - default_timeout_seconds: Maximum time for entire scan operation
    - params_schema: Pydantic model defining parameter structure
    """

    job_type = "network_scan"
    params_schema = NetworkScanParams

    # Job-level timeout: Maximum time for ENTIRE scan (not per-host)
    # Large networks (/16) can take 10+ minutes to scan completely
    default_timeout_seconds = 600  # 10 minutes

    # Execution requirements
    requires_agent = True  # Cannot run on server (needs ping, network access)

    # Display metadata
    display_name = "Network Scan"
    display_description = "Scan a subnet to discover active hosts and open ports"

    async def execute(self) -> dict[str, Any]:
        """
        Execute network scan.

        Uses params_schema for validation and default values.

        Returns:
            Dictionary with scan results:
            {
                "discovered_hosts": [
                    {
                        "ip": "192.168.1.10",
                        "hostname": "printer.local",
                        "responds_to_ping": true,
                        "open_ports": [80, 443]
                    },
                    ...
                ],
                "scan_duration_seconds": 12.5,
                "hosts_scanned": 254,
                "hosts_responding": 12
            }
        """
        scan_start_time = time.time()

        # Validate parameters using Pydantic schema
        try:
            params = self.params_schema(**self.params)
        except Exception as e:
            raise ValueError(f"Invalid parameters: {e}") from e

        subnet = params.subnet
        timeout = params.timeout
        ports = params.ports
        max_concurrent = params.max_concurrent

        # Parse subnet
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid subnet: {subnet} - {e}") from e

        # Limit network size for safety
        if network.num_addresses > 65536:
            raise ValueError(
                f"Network too large ({network.num_addresses} hosts). Maximum is 65536 (/16)"
            )

        self.logger.info(
            "Scanning network",
            extra={
                "subnet": subnet,
                "host_count": network.num_addresses,
                "max_concurrent": max_concurrent,
                "timeout_seconds": timeout,
            },
        )

        # For large networks, break into /24 chunks and scan in parallel
        chunk_metrics = None
        if network.prefixlen < 24 and network.num_addresses > 256:
            total_ips = network.num_addresses - 2  # Exclude network and broadcast
            chunks = list(network.subnets(new_prefix=24))
            self.logger.info(
                "Large network detected",
                extra={"subnet": subnet, "prefix_length": network.prefixlen},
            )
            self.logger.info(
                "Total IPs to scan",
                extra={"total_ips": total_ips, "chunk_count": len(chunks)},
            )
            self.logger.info(
                "Scanning all subnets in parallel",
                extra={
                    "chunk_count": len(chunks),
                    "max_concurrent_per_subnet": max_concurrent,
                },
            )
            discovered_hosts, chunk_metrics = await self._scan_chunked(
                network, timeout, ports, max_concurrent
            )
            hosts_count = total_ips
        else:
            # Small network - scan directly
            hosts = list(network.hosts())
            discovered_hosts = []

            # Create semaphore for concurrency control
            sem = asyncio.Semaphore(max_concurrent)

            async def scan_host(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
                """Scan a single host."""
                async with sem:
                    result = await self._scan_single_host(
                        str(ip),
                        timeout=timeout,
                        ports=ports,
                    )
                    if result:
                        discovered_hosts.append(result)

            # Scan all hosts concurrently
            await asyncio.gather(*[scan_host(ip) for ip in hosts])  # type: ignore[arg-type]
            hosts_count = len(hosts)

        # Sort by IP
        discovered_hosts.sort(key=lambda x: ipaddress.ip_address(x["ip"]))

        # DNS resolution summary
        hosts_with_dns = len([h for h in discovered_hosts if h.get("hostname")])
        hosts_without_dns = len([h for h in discovered_hosts if not h.get("hostname")])

        self.logger.info(
            "Scan complete",
            extra={
                "discovered_count": len(discovered_hosts),
                "total_hosts": hosts_count,
            },
        )
        self.logger.info(
            "DNS Resolution",
            extra={
                "hosts_with_dns": hosts_with_dns,
                "hosts_without_dns": hosts_without_dns,
            },
        )

        # Log sample of failed DNS lookups
        if hosts_without_dns > 0:
            failed_samples = [h["ip"] for h in discovered_hosts if not h.get("hostname")][:5]
            self.logger.info(
                "Sample IPs without DNS",
                extra={"sample_ips": failed_samples},
            )

        # Calculate actual scan duration
        scan_duration = time.time() - scan_start_time

        result = {
            "discovered_hosts": discovered_hosts,
            "scan_duration_seconds": scan_duration,
            "hosts_scanned": hosts_count,
            "hosts_responding": len(discovered_hosts),
            "subnet": subnet,
            "scan_params": {
                "timeout": timeout,
                "ports": ports,
                "max_concurrent": max_concurrent,
            },
        }

        # Add chunk metrics if we did parallel chunked scanning
        if chunk_metrics:
            result["chunk_metrics"] = chunk_metrics
            result["chunks_scanned"] = len(chunk_metrics)

        return result

    async def _scan_chunked(
        self,
        network: ipaddress.IPv4Network | ipaddress.IPv6Network,
        timeout: int,
        ports: list[int],
        max_concurrent: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Scan a large network by breaking it into /24 chunks and scanning in parallel.

        Args:
            network: The large network to scan
            timeout: Ping timeout
            ports: Ports to scan
            max_concurrent: Max concurrent scans per chunk

        Returns:
            List of all discovered hosts across all chunks
        """
        # Break network into /24 subnets
        chunks = list(network.subnets(new_prefix=24))
        start_time = time.time()

        self.logger.info(
            "Starting parallel scan of /24 subnets",
            extra={"chunk_count": len(chunks)},
        )

        # Track progress and metrics
        completed_chunks = 0
        total_discovered = 0
        chunk_metrics = []

        async def scan_chunk(chunk_network, chunk_index):
            """Scan a single /24 chunk."""
            nonlocal completed_chunks, total_discovered

            chunk_start = time.time()
            self.logger.info(
                "Starting subnet chunk",
                extra={
                    "chunk_index": chunk_index + 1,
                    "total_chunks": len(chunks),
                    "chunk_network": str(chunk_network),
                },
            )

            chunk_hosts = []
            hosts = list(chunk_network.hosts())

            # Create semaphore for this chunk
            sem = asyncio.Semaphore(max_concurrent)

            async def scan_host(ip):
                async with sem:
                    result = await self._scan_single_host(str(ip), timeout, ports)
                    if result:
                        chunk_hosts.append(result)

            await asyncio.gather(*[scan_host(ip) for ip in hosts])

            chunk_duration = time.time() - chunk_start
            completed_chunks += 1
            total_discovered += len(chunk_hosts)

            # Store chunk metrics
            chunk_metrics.append(
                {
                    "subnet": str(chunk_network),
                    "duration_seconds": round(chunk_duration, 2),
                    "hosts_scanned": len(hosts),
                    "hosts_found": len(chunk_hosts),
                }
            )

            self.logger.info(
                "Subnet chunk complete",
                extra={
                    "chunk_index": chunk_index + 1,
                    "total_chunks": len(chunks),
                    "chunk_network": str(chunk_network),
                    "chunk_duration_seconds": round(chunk_duration, 1),
                    "chunk_hosts_found": len(chunk_hosts),
                    "chunk_hosts_total": len(hosts),
                    "completed_chunks": completed_chunks,
                    "total_discovered": total_discovered,
                },
            )
            return chunk_hosts

        # Scan all chunks concurrently
        self.logger.info(
            "Scanning chunks concurrently",
            extra={"chunk_count": len(chunks)},
        )
        results = await asyncio.gather(*[scan_chunk(chunk, i) for i, chunk in enumerate(chunks)])

        # Flatten results
        all_hosts = []
        for chunk_results in results:
            all_hosts.extend(chunk_results)

        total_duration = time.time() - start_time
        self.logger.info(
            "Parallel scan complete",
            extra={
                "total_duration_seconds": round(total_duration, 1),
                "total_hosts": len(all_hosts),
                "subnet_count": len(chunks),
            },
        )

        # Sort chunk metrics by subnet for readability
        chunk_metrics.sort(key=lambda x: ipaddress.ip_network(x["subnet"]))

        return all_hosts, chunk_metrics

    async def _scan_single_host(
        self,
        ip: str,
        timeout: int,
        ports: list[int],
    ) -> dict[str, Any] | None:
        """
        Scan a single host for responsiveness.

        Args:
            ip: IP address to scan
            timeout: Ping timeout in seconds
            ports: List of ports to check

        Returns:
            Host info dict if responsive, None otherwise
        """
        # Try ping
        responds_to_ping = await self._ping_host(ip, timeout)

        if not responds_to_ping:
            # Skip if host doesn't respond to ping
            return None

        # Resolve hostname
        hostname = await self._resolve_hostname(ip, timeout)

        # Scan ports if requested
        open_ports = []
        if ports:
            open_ports = await self._scan_ports(ip, ports, timeout)

        return {
            "ip": ip,
            "hostname": hostname,
            "responds_to_ping": responds_to_ping,
            "open_ports": open_ports,
        }

    async def _ping_host(self, ip: str, timeout: int) -> bool:
        """
        Ping a host to check if it's responsive.

        Args:
            ip: IP address
            timeout: Timeout in seconds

        Returns:
            True if host responds, False otherwise
        """
        subprocess_config = get_subprocess_config({})

        # Build platform-specific ping command
        system = platform.system().lower()

        if system == "windows":
            cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), ip]
        else:
            # Linux, macOS, etc.
            cmd = ["ping", "-c", "1", "-W", str(timeout), ip]

        try:
            returncode = await run_subprocess_no_output(
                *cmd,
                timeout=float(timeout),
                grace_seconds=subprocess_config["grace_seconds"],
                kill_timeout=subprocess_config["kill_timeout"],
            )
            return returncode == 0

        except TimeoutError, OSError:
            return False

    async def _resolve_hostname(self, ip: str, timeout: int = 5) -> str | None:
        """
        Resolve IP to hostname via reverse DNS.

        Args:
            ip: IP address
            timeout: DNS lookup timeout in seconds

        Returns:
            Hostname or None if not resolvable
        """
        try:
            self.logger.debug("Starting DNS lookup", extra={"ip": ip})
            # Run blocking gethostbyaddr in thread pool
            loop = asyncio.get_event_loop()
            hostname, _, _ = await asyncio.wait_for(
                loop.run_in_executor(None, socket.gethostbyaddr, ip),
                timeout=float(timeout),
            )
            self.logger.debug(
                "DNS SUCCESS",
                extra={"ip": ip, "hostname": hostname},
            )
            return hostname
        except socket.herror:
            self.logger.debug(
                "DNS herror",
                extra={"ip": ip},
                exc_info=True,
            )
            return None
        except socket.gaierror:
            self.logger.debug(
                "DNS gaierror",
                extra={"ip": ip},
                exc_info=True,
            )
            return None
        except TimeoutError:
            self.logger.warning(
                "DNS TIMEOUT",
                extra={"ip": ip, "timeout_seconds": timeout},
            )
            return None
        except OSError:
            self.logger.debug(
                "DNS OSError",
                extra={"ip": ip},
                exc_info=True,
            )
            return None
        except Exception:
            self.logger.error(
                "DNS unexpected error",
                extra={"ip": ip},
                exc_info=True,
            )
            return None

    async def _scan_ports(
        self,
        ip: str,
        ports: list[int],
        timeout: int,
    ) -> list[int]:
        """
        Scan ports on a host.

        Args:
            ip: IP address
            ports: List of ports to check
            timeout: Connection timeout in seconds

        Returns:
            List of open ports
        """
        open_ports = []

        async def check_port(port: int):
            """Check if a single port is open."""
            try:
                # Try to connect
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port),
                    timeout=timeout,
                )
                writer.close()
                await writer.wait_closed()
                open_ports.append(port)
            except TimeoutError, OSError, ConnectionRefusedError:
                # Port closed or filtered
                pass

        # Check all ports concurrently
        await asyncio.gather(*[check_port(port) for port in ports])

        return sorted(open_ports)
