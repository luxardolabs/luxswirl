"""On the agent, a PostgreSQL unix socket follows Block Private Network Targets, and an
address naming too many hosts is a configuration error.

A unix socket reaches the agent host's own services, as loopback does, so with the
block on it is refused before the driver connects. Both checks run through
`shared.ssrf`, which the server's save also uses.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.checks import _ssrf_policy
from app.checks.base import CheckConfigError
from app.shared.ssrf import MAX_TARGET_HOSTS, SsrfBlockedError


@pytest.fixture(autouse=True)
def _policy() -> Iterator[None]:
    _ssrf_policy.apply_server_policy({"block_private_networks": True})
    yield
    _ssrf_policy.apply_server_policy({"block_private_networks": False})


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://u:pw@/d?host=/run/postgresql",
        "postgresql://u:pw@%2Fvar%2Frun%2Fpostgresql/d",
        "postgresql://u:pw@/d?host=/tmp,/run/x",
    ],
)
def test_a_unix_socket_is_refused_with_private_networks_blocked(dsn: str) -> None:
    with pytest.raises(SsrfBlockedError, match="unix socket") as refused:
        _ssrf_policy.assert_target_allowed(dsn, database=True)
    assert "pw" not in str(refused.value)


def test_a_unix_socket_is_allowed_with_private_networks_allowed() -> None:
    _ssrf_policy.apply_server_policy({"block_private_networks": False})
    _ssrf_policy.assert_target_allowed(
        "postgres://u:pw@/d?host=/run/postgresql", database=True
    )


def test_an_address_naming_too_many_hosts_is_a_config_error() -> None:
    hosts = ",".join(f"10.0.0.{n}" for n in range(MAX_TARGET_HOSTS + 1))
    with pytest.raises(CheckConfigError, match="hosts"):
        _ssrf_policy.assert_target_allowed(
            f"postgresql://u:pw@{hosts}/d", database=True
        )
