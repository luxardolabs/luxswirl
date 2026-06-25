"""
URL security validation for agent-server communication.

Enforces HTTPS for external servers to prevent credential theft.
"""

import os
from urllib.parse import urlparse

from shared.logger import get_logger

logger = get_logger("luxswirl.security.url")


def validate_server_url(url: str) -> None:
    """
    Validate server URL and enforce HTTPS for external connections.

    Internal/local destinations (safe for HTTP):
    - localhost, 127.0.0.1
    - Docker service names (server, luxswirl_server)
    - Private IP ranges (10.x.x.x, 172.16-31.x.x, 192.168.x.x)
    - .local domains (mDNS)

    External destinations (HTTPS required):
    - Public hostnames
    - Public IP addresses

    Args:
        url: The server URL to validate

    Raises:
        ValueError: If external HTTP URL is used without explicit override
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid URL format: {url} - {e}") from e

    # Check if URL has a scheme
    if not parsed.scheme:
        raise ValueError(f"URL missing scheme (http:// or https://): {url}")

    # Check if URL has a hostname
    if not parsed.hostname:
        raise ValueError(f"URL missing hostname: {url}")

    # HTTPS is always allowed
    if parsed.scheme == "https":
        logger.debug(
            "Secure HTTPS connection",
            extra={"server_hostname": parsed.hostname},
        )
        return

    # HTTP requires validation
    if parsed.scheme == "http":
        if _is_internal_destination(parsed.hostname):
            logger.debug(
                "Internal HTTP connection allowed",
                extra={"server_hostname": parsed.hostname},
            )
            return

        # External HTTP - check for explicit override
        if os.getenv("LUXSWIRL_ALLOW_INSECURE_HTTP") == "true":
            logger.warning(
                "\n" + "=" * 70 + "\n"
                "⚠️  SECURITY WARNING: INSECURE HTTP ENABLED ⚠️\n"
                f"Connecting to external server via HTTP: {url}\n"
                "Credentials will be transmitted in CLEARTEXT!\n"
                "This is UNSAFE for production. Use HTTPS.\n"
                "=" * 70 + "\n"
            )
            return

        # External HTTP without override - BLOCKED
        raise ValueError(
            f"HTTPS required for external server: {parsed.hostname}\n"
            f"Current URL: {url}\n\n"
            f"Fix: Use HTTPS URL\n"
            f"  Set: LUXSWIRL_SERVER_URL=https://{parsed.hostname}:{parsed.port or 9000}\n\n"
            f"For testing ONLY, override with: LUXSWIRL_ALLOW_INSECURE_HTTP=true\n"
            f"WARNING: This sends credentials in cleartext. Never use in production."
        )

    # Unknown scheme
    raise ValueError(f"Invalid URL scheme '{parsed.scheme}'. Must be http or https: {url}")


def _is_internal_destination(hostname: str) -> bool:
    """
    Check if hostname is an internal/local destination safe for HTTP.

    Args:
        hostname: The hostname to check

    Returns:
        True if internal/local, False if external
    """
    # Localhost
    if hostname in ["localhost", "127.0.0.1", "::1"]:
        return True

    # Docker service names (common patterns)
    if hostname in ["server", "luxswirl_server", "luxswirl-server"]:
        return True

    # .local domains (mDNS)
    if hostname.endswith(".local"):
        return True

    # Docker internal domains
    if hostname.endswith(".internal"):
        return True

    # Check if it's a private IP address
    if _is_private_ip(hostname):
        return True

    return False


def _is_private_ip(hostname: str) -> bool:
    """
    Check if hostname is a private IP address (RFC 1918).

    Private ranges:
    - 10.0.0.0/8 (10.x.x.x)
    - 172.16.0.0/12 (172.16.x.x - 172.31.x.x)
    - 192.168.0.0/16 (192.168.x.x)

    Args:
        hostname: The hostname/IP to check

    Returns:
        True if private IP, False otherwise
    """
    try:
        # Split into octets
        parts = hostname.split(".")
        if len(parts) != 4:
            return False

        # Convert to integers
        octets = [int(part) for part in parts]

        # Validate range
        if not all(0 <= octet <= 255 for octet in octets):
            return False

        # Check private ranges
        # 10.0.0.0/8
        if octets[0] == 10:
            return True

        # 172.16.0.0/12 (172.16 - 172.31)
        if octets[0] == 172 and 16 <= octets[1] <= 31:
            return True

        # 192.168.0.0/16
        if octets[0] == 192 and octets[1] == 168:
            return True

        return False

    except ValueError, IndexError:
        # Not a valid IP address
        return False
