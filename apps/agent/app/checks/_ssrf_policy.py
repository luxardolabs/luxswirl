"""The SSRF policy every check enforces when it connects, as the server sets it.

The cloud-metadata range is always refused. Private networks are refused when
the server's ``security.block_private_networks`` setting is on: the checks list
and every heartbeat response carry it, so a change reaches the agent within one
heartbeat, and the checks list sets it before any check first runs.
"""

from collections.abc import Mapping
from typing import Any

from app.checks.base import CheckConfigError
from app.shared import ssrf
from app.shared.logger import get_logger

logger = get_logger("luxswirl.security.ssrf")

_policy = {"block_private_networks": False}


def block_private_networks() -> bool:
    """Whether checks refuse private-network addresses."""
    return _policy["block_private_networks"]


def apply_server_policy(response: Mapping[str, Any]) -> None:
    """Take the policy from a server response; a response without it changes nothing."""
    value = response.get("block_private_networks")
    if not isinstance(value, bool) or value == _policy["block_private_networks"]:
        return
    _policy["block_private_networks"] = value
    logger.info(
        "SSRF policy updated from the server",
        extra={"block_private_networks": value},
    )


def _vet_host_name(host: str) -> None:
    """Refuse a host name no resolver can take: one with a null character, or
    one the IDNA codec cannot encode (a label over 63 octets, an empty label).

    Raises:
        CheckConfigError: The name cannot be resolved as written.
    """
    if "\x00" in host:
        raise CheckConfigError(f"Invalid host name {host!r}: it holds a null character")
    try:
        host.encode("idna")
    except UnicodeError as e:
        raise CheckConfigError(f"Invalid host name {host!r}: {e}") from e


def assert_target_allowed(target: str) -> None:
    """Refuse `target` if ANY address it resolves to is in a range the policy
    blocks: for a check whose driver resolves the name itself.

    Raises:
        CheckConfigError: A host it names cannot be resolved as written.
        SsrfBlockedError: An address a host resolves to is blocked.
    """
    for host in ssrf.extract_hosts(target):
        _vet_host_name(host)
    ssrf.assert_target_allowed(
        target,
        block_cloud_metadata=True,
        block_private_networks=block_private_networks(),
    )


def allowed_addresses(host: str) -> list[str]:
    """The addresses `host` resolves to that the policy allows, to connect to.

    Raises:
        CheckConfigError: `host` cannot be resolved as written.
        SsrfBlockedError: It resolves, and every address is blocked.
    """
    _vet_host_name(host)
    return ssrf.allowed_addresses(
        host,
        block_cloud_metadata=True,
        block_private_networks=block_private_networks(),
    )


def ip_allowed(ip: str) -> bool:
    """Whether the policy allows connecting to the resolved `ip`."""
    return (
        ssrf.blocked_range(
            ip,
            block_cloud_metadata=True,
            block_private_networks=block_private_networks(),
        )
        is None
    )


def assert_ip_allowed(ip: str) -> None:
    """Refuse the resolved `ip` if it is in a range the policy blocks."""
    ssrf.assert_ip_allowed(
        ip,
        block_cloud_metadata=True,
        block_private_networks=block_private_networks(),
    )
