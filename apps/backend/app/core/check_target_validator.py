"""Check target URL/host validation for SSRF protection (server side).

Thin wrapper over ``shared.ssrf`` so the server (validates at check create/update
time) and the agent (re-validates at fetch time) share one set of blocked ranges
and one resolver. Raises ``CheckTargetBlockedError`` — the server's exception type,
which the API and web error layers already translate to a 4xx.

Settings (Settings > Security):
- security.block_cloud_metadata (default True) — blocks 169.254.0.0/16 + IPv6 link-local
- security.block_private_networks (default False) — blocks RFC 1918 + IPv6 ULA
"""

from shared.ssrf import SsrfBlockedError, assert_target_allowed


class CheckTargetBlockedError(ValueError):
    """Raised when a check target is blocked by SSRF protection."""


def validate_check_target(
    target: str,
    *,
    block_cloud_metadata: bool = True,
    block_private_networks: bool = False,
) -> None:
    """Validate a check target against SSRF rules at check create/update time.

    Delegates to ``shared.ssrf`` — the same ranges and resolver the agent enforces
    at fetch time — and raises :class:`CheckTargetBlockedError` if the target
    resolves into a blocked range.

    Args:
        target: The check target (URL or ``host:port``).
        block_cloud_metadata: Block cloud-metadata / link-local addresses.
        block_private_networks: Block RFC 1918 / IPv6 ULA addresses.
    """
    try:
        assert_target_allowed(
            target,
            block_cloud_metadata=block_cloud_metadata,
            block_private_networks=block_private_networks,
        )
    except SsrfBlockedError as e:
        raise CheckTargetBlockedError(
            f"{e} Adjust SSRF protection under Settings > Security to allow it."
        ) from e
