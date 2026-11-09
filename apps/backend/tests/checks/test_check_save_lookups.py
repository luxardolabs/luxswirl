"""Saving a check: what it may name, and where its lookups run.

A PostgreSQL URL can name a unix socket, which reaches this host's own
services, as loopback does: with private networks blocked it is refused, at save
and on the agent (both through `shared.ssrf`). An address naming more hosts than
`MAX_TARGET_HOSTS` is refused before any lookup, and the lookups a save makes
run off the event loop, so a slow nameserver stalls no other request.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import pytest
from app.core.check_target_validator import CheckTargetBlockedError
from app.schemas.check_schema import CheckCreate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared import ssrf
from app.shared.ssrf import MAX_TARGET_HOSTS, SsrfBlockedError, assert_target_allowed
from fixtures.factories import make_agent
from sqlalchemy.ext.asyncio import AsyncSession

UNIX_SOCKET_URLS = [
    "postgres://u:s3cret@/d?host=/run/x",
    "postgresql://u:s3cret@%2Frun%2Fpostgresql/d",
    "postgresql://u:s3cret@%2Frun%2Fpostgresql:5432/d",
    "postgresql://u:s3cret@db.example.com,%2Ftmp/d",
    "postgres://u:s3cret@db.example.com/d?host=/var/run/postgresql",
    "postgresql://u:s3cret@/d?host=db.example.com,/tmp",
]


@pytest.mark.unit
@pytest.mark.parametrize("url", UNIX_SOCKET_URLS)
def test_a_unix_socket_is_refused_when_private_networks_are_blocked(url: str) -> None:
    with pytest.raises(SsrfBlockedError, match="private network") as refused:
        assert_target_allowed(url, block_private_networks=True, database=True)
    assert "s3cret" not in str(refused.value)


@pytest.mark.unit
@pytest.mark.parametrize("url", UNIX_SOCKET_URLS)
def test_a_unix_socket_is_allowed_when_private_networks_are_allowed(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssrf, "resolve_host", lambda _host: ["192.0.2.10"])
    assert_target_allowed(url, database=True)


@pytest.mark.unit
def test_a_url_naming_too_many_hosts_is_refused_before_any_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[str] = []
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: looked_up.append(host) or [])
    hosts = ",".join(f"h{n}.example.com" for n in range(MAX_TARGET_HOSTS + 1))

    with pytest.raises(SsrfBlockedError, match="hosts") as refused:
        assert_target_allowed(f"postgresql://u:s3cret@{hosts}/d", database=True)

    assert looked_up == []
    assert "s3cret" not in str(refused.value)


@pytest.mark.unit
def test_a_url_naming_the_most_hosts_allowed_is_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssrf, "resolve_host", lambda _host: ["192.0.2.10"])
    hosts = ",".join(f"h{n}.example.com" for n in range(MAX_TARGET_HOSTS))
    assert_target_allowed(f"postgresql://u:p@{hosts}/d", database=True)


def _postgres(connection_string: str) -> CheckCreate:
    return CheckCreate(
        display_name="save-lookups",
        check_type="postgres",
        target="192.0.2.10",
        connection_string=connection_string,
        interval_seconds=60,
        timeout_seconds=5,
    )


@pytest.mark.integration
async def test_saving_a_unix_socket_check_follows_the_setting(
    db: AsyncSession,
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", True
    )
    agent = make_agent()
    db.add(agent)
    await db.flush()

    with pytest.raises(CheckTargetBlockedError) as refused:
        await CheckCoreService.create_check(
            db, agent.id, _postgres("postgres://u:s3cret@/d?host=/run/x")
        )
    assert "s3cret" not in str(refused.value)


@pytest.mark.integration
async def test_saving_a_check_naming_too_many_hosts_is_refused(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    hosts = ",".join(f"h{n}.example.com" for n in range(MAX_TARGET_HOSTS + 1))

    with pytest.raises(CheckTargetBlockedError, match="hosts"):
        await CheckCoreService.create_check(
            db, agent.id, _postgres(f"postgresql://u:s3cret@{hosts}/d")
        )


@pytest.mark.integration
async def test_a_slow_lookup_on_save_leaves_the_event_loop_free(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_lookup(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(0.4)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", slow_lookup)
    agent = make_agent()
    db.add(agent)
    await db.flush()
    gaps: list[float] = []
    saving = True

    async def ticker() -> None:
        last = time.monotonic()
        while saving:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticks = asyncio.create_task(ticker())
    await CheckCoreService.create_check(
        db, agent.id, _postgres("postgresql://u:p@a.example.com,b.example.com/d")
    )
    saving = False
    await ticks

    assert sum(gaps) >= 0.8  # both lookups ran while the ticker did
    assert max(gaps) < 0.2
