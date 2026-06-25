"""
TCP Check Module - Implements TCP connection health checks.
"""

import asyncio
import socket
from typing import Any

from shared.ssrf import assert_ip_allowed

from app.checks.base import BaseCheck


class TCPCheck(BaseCheck):
    """Check for TCP port availability."""

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate TCP-specific configuration.

        Args:
            config: The check configuration to validate

        Raises:
            ValueError: If required configuration fields are missing
        """
        super().validate_config(config)

        if "port" not in config:
            raise ValueError(f"TCP check {config.get('name', 'unnamed')} must have a 'port'")

        port = config["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError(f"TCP port must be an integer between 1 and 65535: {port}")

    async def run(self) -> dict[str, Any]:
        """Execute the TCP connection check.

        Returns:
            A dictionary containing the check result
        """
        host = self.config["target"]
        port = self.config["port"]
        timeout = self.config.get("timeout", 2)
        retries = self.config.get("retries", 1)

        # Optional config parameters
        send_string = self.config.get("send_string")
        expect_string = self.config.get("expect_string")

        success = False
        latency_ms = None
        error = None
        response = None
        additional_data: dict[str, Any] = {}

        # Try connecting with retries
        for _attempt in range(retries):
            try:
                start_time = self.start_timer()

                # Get address info to handle both IPv4 and IPv6
                info = await self._get_address_info(host, port)
                if not info:
                    error = f"Could not resolve host: {host}"
                    continue

                address_family, socktype, proto, _, socket_address = info

                # SSRF: validate the exact resolved IP we're about to connect to
                # (pinned — no re-resolution gap) before opening the socket.
                assert_ip_allowed(socket_address[0], block_cloud_metadata=True)

                # Open connection
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=socket_address[0],
                        port=socket_address[1],
                        family=address_family,
                        proto=proto,
                    ),
                    timeout=timeout,
                )

                # Calculate initial connection latency
                latency_ms = self.stop_timer(start_time)
                additional_data["connect_latency_ms"] = latency_ms

                # If we need to send/receive data
                if send_string or expect_string:
                    response, echo_latency = await self._test_echo(
                        reader, writer, send_string, expect_string, timeout
                    )
                    if echo_latency is not None:
                        additional_data["echo_latency_ms"] = echo_latency
                    if response is not None:
                        additional_data["response"] = response

                # Close the connection
                writer.close()
                await writer.wait_closed()

                # If we get here, the check succeeded
                success = True
                break

            except TimeoutError:
                error = f"Connection timed out after {timeout}s"
                continue
            except ConnectionRefusedError:
                error = "Connection refused"
                continue
            except socket.gaierror as e:
                error = f"Address resolution error: {str(e)}"
                continue
            except OSError as e:
                error = f"Socket error: {str(e)}"
                continue
            except Exception as e:
                error = f"Unexpected error: {str(e)}"
                continue

        # Create the result
        return self.create_result(
            success=success,
            latency_ms=latency_ms,
            error=error,
            target_host=host,
            target_port=port,
            **additional_data,
        )

    async def _get_address_info(self, host: str, port: int) -> tuple | None:
        """Get socket address information.

        Args:
            host: The hostname or IP address
            port: The port number

        Returns:
            The first valid address info tuple or None if resolution fails
        """
        try:
            # Use getaddrinfo to support both IPv4 and IPv6
            info = await asyncio.get_event_loop().getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
            if info:
                return info[0]  # Return the first valid address info
            return None
        except socket.gaierror:
            return None

    async def _test_echo(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        send_string: str | None,
        expect_string: str | None,
        timeout: float,
    ) -> tuple[str | None, float | None]:
        """Test echo functionality by sending and receiving data.

        Args:
            reader: The stream reader
            writer: The stream writer
            send_string: The string to send, if any
            expect_string: The expected response string, if any
            timeout: Timeout in seconds

        Returns:
            A tuple of (response_string, latency_ms)
        """
        response = None
        latency = None

        if not send_string:
            return response, latency

        try:
            # Send data
            start_time = self.start_timer()
            writer.write(send_string.encode())
            await writer.drain()

            # Read response if expect_string is set
            if expect_string:
                response_data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
                response = response_data.decode("utf-8", errors="replace")
                latency = self.stop_timer(start_time)

                # Check if response matches expected
                if response and expect_string not in response:
                    raise ValueError("Response does not match expected string")

        except TimeoutError:
            raise ValueError("Timed out waiting for response") from None
        except Exception as e:
            raise ValueError(f"Echo test failed: {str(e)}") from e

        return response, latency
