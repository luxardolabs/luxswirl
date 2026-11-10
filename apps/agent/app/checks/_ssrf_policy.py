"""The SSRF policy every check enforces when it connects, as the server sets it.

The cloud-metadata range is always refused. Private networks are refused when
the server's ``security.block_private_networks`` setting is on: the checks list
and every heartbeat response carry it, so a change reaches the agent within one
heartbeat, and the checks list sets it before any check first runs.
"""

from collections.abc import Mapping
from typing import Any

from app.checks._lookups import LookupTimeoutError, run_blocking
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


def assert_target_allowed(target: str, *, database: bool = False) -> None:
    """Refuse `target` if ANY address it resolves to is in a range the policy
    blocks: for a check whose driver resolves the name itself. `database`: the
    driver is a database's, which reads a URL with no `//` too. It blocks while
    the names resolve: a check awaits `target_allowed` instead.

    Raises:
        CheckConfigError: A host it names cannot be resolved as written, or a
            database driver would read it and its hosts cannot be read plainly:
            the check's settings are what is wrong.
        SsrfBlockedError: An address a host resolves to is blocked.
    """
    _target_allowed_under(target, database, block_private_networks())


async def target_allowed(target: str, *, database: bool = False) -> None:
    """`assert_target_allowed`, its lookups on the checks' lookup pool. A target
    judged under a policy the server changed while its names resolved is judged
    again under the new one.

    Raises:
        CheckConfigError: As `assert_target_allowed`.
        SsrfBlockedError: As `assert_target_allowed`.
        LookupTimeoutError: A lookup took over `LOOKUP_SECONDS`; it names the hosts.
    """
    judged_under = None
    while judged_under != (policy := block_private_networks()):
        try:
            await run_blocking(_target_allowed_under, target, database, policy)
        except LookupTimeoutError as e:
            hosts = ", ".join(ssrf.extract_hosts(target, database=database))
            raise LookupTimeoutError(f"Could not resolve host: {hosts} ({e})") from None
        judged_under = policy


def _target_allowed_under(target: str, database: bool, block_private: bool) -> None:
    try:
        hosts = ssrf.extract_hosts(target, database=database)
    except ssrf.UnallowableTargetError as e:
        raise CheckConfigError(str(e)) from None
    for host in hosts:
        _vet_host_name(host)
    ssrf.assert_target_allowed(
        target,
        block_cloud_metadata=True,
        block_private_networks=block_private,
        database=database,
    )


def allowed_addresses(host: str) -> list[str]:
    """The addresses `host` resolves to that the policy allows, to connect to.
    It blocks while the name resolves: run it on the lookup pool.

    Raises:
        CheckConfigError: `host` cannot be resolved as written.
        SsrfBlockedError: It resolves, and every address is blocked.
    """
    return _allowed_under(host, block_private_networks())


async def lookup_allowed_addresses(host: str) -> list[str]:
    """`allowed_addresses`, on the checks' lookup pool.

    Raises:
        CheckConfigError: `host` cannot be resolved as written.
        SsrfBlockedError: It resolves, and every address is blocked.
        LookupTimeoutError: The lookup took over `LOOKUP_SECONDS`; it names `host`.
    """
    try:
        addresses = await run_blocking(_allowed_under, host, block_private_networks())
    except LookupTimeoutError as e:
        raise LookupTimeoutError(f"Could not resolve host: {host} ({e})") from None
    # Judged again under the policy as it is now: the server can change it while
    # the name resolves.
    allowed = [address for address in addresses if ip_allowed(address)]
    if addresses and not allowed:
        assert_ip_allowed(addresses[0])
    return allowed


def _allowed_under(host: str, block_private: bool) -> list[str]:
    _vet_host_name(host)
    return ssrf.allowed_addresses(
        host,
        block_cloud_metadata=True,
        block_private_networks=block_private,
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
