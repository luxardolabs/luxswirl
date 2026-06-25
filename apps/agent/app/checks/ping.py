"""
Ping Check Module - Implements ICMP ping health checks.
"""

import platform
import re
from typing import Any

from shared.ssrf import assert_target_allowed
from shared.subprocess_safe import get_subprocess_config, run_subprocess_safely

from app.checks.base import BaseCheck


class PingCheck(BaseCheck):
    """Check for host availability using ICMP ping."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate ping-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing
        """
        super().validate_config(config)

        # Check that target is not an HTTP URL
        target = config.get("target", "")
        if target.startswith(("http://", "https://")):
            raise ValueError(f"Ping check target should not include protocol: {target}")

    async def run(self) -> dict[str, Any]:
        """Execute the ping health check.

        Returns:
            A dictionary containing the check result
        """
        host = self.config["target"]
        # SSRF: refuse a ping target that resolves into the cloud-metadata range.
        assert_target_allowed(host, block_cloud_metadata=True)
        count = self.config.get("count", 1)
        timeout = self.config.get("timeout", 1)
        retries = self.config.get("retries", 1)

        success = False
        ping_stats = None
        error = None

        # Get subprocess config for safe execution
        subprocess_config = get_subprocess_config(self.config)

        # Adjust parameters based on OS
        ping_args = self._get_ping_args(host, count, timeout)

        # Try pinging with retries
        for _attempt in range(retries):
            try:
                # Run the ping command with safe subprocess wrapper
                # timeout includes the count * ping_timeout plus overhead from config
                # e.g., 3 pings x 1s each = 3s minimum, plus grace period
                subprocess_timeout = (count * timeout) + subprocess_config["grace_seconds"]

                returncode, stdout, stderr = await run_subprocess_safely(
                    *ping_args,
                    timeout=subprocess_timeout,
                    capture_output=True,
                    grace_seconds=subprocess_config["grace_seconds"],
                    kill_timeout=subprocess_config["kill_timeout"],
                )

                # Check return code
                if returncode == 0:
                    # Parse the output to get ping statistics
                    ping_output = (stdout or b"").decode("utf-8", errors="replace")
                    ping_stats = self._parse_ping_output(ping_output)
                    success = True
                    break
                else:
                    stderr_output = (stderr or b"").decode("utf-8", errors="replace")
                    error = f"Ping failed with return code {returncode}: {stderr_output}"

            except TimeoutError:
                error = f"Ping timed out after {timeout}s"
                continue
            except Exception as e:
                error = f"Ping failed: {str(e)}"
                continue

        # Get packet loss from stats or set to 100% if failed
        packet_loss = ping_stats.get("packet_loss", 100) if ping_stats else 100

        # Create and return the result
        return self.create_result(
            success=success,
            latency_ms=ping_stats.get("avg_ms") if ping_stats else None,
            error=error,
            target_host=host,
            packet_loss=packet_loss,
            ping_statistics=ping_stats,
        )

    def _get_ping_args(self, host: str, count: int, timeout: float) -> list[str]:
        """Get the appropriate ping command arguments based on the OS.

        Args:
            host: The hostname or IP to ping
            count: Number of ping packets to send
            timeout: Timeout in seconds

        Returns:
            A list of command line arguments for the ping command
        """
        os_name = platform.system().lower()

        if os_name == "windows":
            # Windows ping
            return [
                "ping",
                "-n",
                str(count),
                "-w",
                str(int(timeout * 1000)),  # Windows uses milliseconds
                host,
            ]
        elif os_name == "darwin":
            # macOS ping
            return ["ping", "-c", str(count), "-t", str(int(timeout)), host]
        else:
            # Linux ping
            return ["ping", "-c", str(count), "-W", str(int(timeout)), host]

    def _parse_ping_output(self, output: str) -> dict[str, Any]:
        """Parse ping command output to extract statistics.

        Args:
            output: The stdout output from the ping command

        Returns:
            A dictionary containing ping statistics
        """
        stats: dict[str, Any] = {
            "sent": 0,
            "received": 0,
            "packet_loss": 100.0,
            "min_ms": None,
            "avg_ms": None,
            "max_ms": None,
            "stddev_ms": None,
            "raw_output": output,
            "times": [],
        }

        # Parse packet statistics
        packet_loss_match = re.search(r"(\d+)% packet loss", output)
        if packet_loss_match:
            stats["packet_loss"] = float(packet_loss_match.group(1))

        # Parse packets sent/received
        packets_match = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
        if packets_match:
            stats["sent"] = int(packets_match.group(1))
            stats["received"] = int(packets_match.group(2))

        # Parse round-trip times
        rtt_match = re.search(
            r"min/avg/max(?:/mdev|/stddev)? = ([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))?",
            output,
        )
        if rtt_match:
            stats["min_ms"] = float(rtt_match.group(1))
            stats["avg_ms"] = float(rtt_match.group(2))
            stats["max_ms"] = float(rtt_match.group(3))
            if rtt_match.group(4):
                stats["stddev_ms"] = float(rtt_match.group(4))

        # Extract individual ping times
        times = []
        time_matches = re.finditer(r"time=([\d.]+) ms", output)
        for match in time_matches:
            times.append(float(match.group(1)))

        if times:
            stats["times"] = times

            # If we didn't get the avg from the summary, calculate it
            if stats["avg_ms"] is None and times:
                stats["avg_ms"] = sum(times) / len(times)

        return stats
